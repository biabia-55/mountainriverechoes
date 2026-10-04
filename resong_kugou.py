#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""酷狗存量音源批量迁移: 酷我 → B站 → 微信公众号 → (保留酷狗)

只处理指定族库里 source=KugouMusicClient 的曲目, 按用户优先级逐首找替身:
  1. 酷我   : 歌手候选池 + 精确同曲判定(_same_recording), 直链 http 有效
  2. B站    : 标题双包含(歌名+歌手), 只收 ytdlp: 形态(永不过期)
  3. 微信    : 搜狗微信搜索, _same_recording 判定(mpvoice 永久直链)
  4. 全失败  : 保留酷狗原样(不计入断点, 下轮自动重试)

限流对策:
  - 非阻塞探针: 源被限流时进入 paused_until, 到点自动重试(不阻塞其他源的工作)
  - 按歌手建池: 同一歌手的多首曲目共享一次搜索结果(约省 69% 搜索量)
  - 断点续跑: /tmp/resong_state.json 只记录成功/跳过, 重跑自动跳过
  - 每批原子写盘(写前重读文件, 与常驻服务互不覆盖)
"""
import sys, os, json, time, re, argparse, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from webui.mountainriverechoes import (
    _dedup_key, _atomic_write_json, _cache_path, _search_with_timeout,
    _same_recording, _norm_str, ETHNIC_BY_KEY,
)
from musicdl import musicdl
from musicdl.modules import SongInfo
from musicdl.modules.sources.weixin import WeixinMusicClient, WeixinSogouBlocked

DEFAULT_GROUPS = ['e19', 'e26', 'e16', 'e22', 'e18', 'e02', 'e05']
STATE_FILE = '/tmp/resong_state.json'
LOG_FILE = '/tmp/resong.log'
_lock = threading.Lock()

log_fp = open(LOG_FILE, 'a', buffering=1, encoding='utf-8')
def log(msg):
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    log_fp.write(line + '\n')

# ---------------- 非阻塞熔断器 ----------------
class Breaker:
    """零结果累计到阈值 -> 进入 paused_until(到点自动重试); 反复熔断超过上限才停用"""
    def __init__(self, name, pause, threshold, max_pauses):
        self.name, self.pause, self.threshold = name, pause, threshold
        self.max_pauses = max_pauses
        self.zeros = 0
        self.pauses = 0
        self.paused_until = 0
        self.disabled = False
    def ready(self):
        return not self.disabled and time.time() >= self.paused_until
    def note_zero(self):
        if self.disabled: return
        self.zeros += 1
        if self.zeros >= self.threshold:
            if self.pauses < self.max_pauses:
                self.paused_until = time.time() + self.pause
                self.pauses += 1
                self.zeros = 0
                log(f'[{self.name}] 连续零结果 x{self.threshold}, 暂停到 {time.strftime("%H:%M:%S", time.localtime(self.paused_until))} (第 {self.pauses}/{self.max_pauses} 次)')
            else:
                self.disabled = True
                log(f'[{self.name}] 暂停次数耗尽, 本轮停用')
    def note_hit(self):
        self.zeros = 0

# ---------------- 按歌手建池的搜索层 ----------------
class PooledSearch:
    def __init__(self, client, src, breaker, sleep_s):
        self.client, self.src, self.breaker, self.sleep_s = client, src, breaker, sleep_s
        self.pool = {}          # singer_norm -> [SongInfo]
        self.pool_empty = set() # 池搜过但空

    def _pool_for(self, song):
        s0 = str(song.singers or '').split('/')[0].split(',')[0].split('、')[0].strip()
        key = _norm_str(s0)
        if not key: return s0, None
        if key in self.pool or key in self.pool_empty:
            return s0, self.pool.get(key) or []
        if not self.breaker.ready():
            return s0, None
        try:
            cands = _search_with_timeout(self.client, self.src, s0, 20) or []
        except Exception:
            cands = []
        time.sleep(self.sleep_s)
        if cands:
            self.pool[key] = cands
            self.breaker.note_hit()
        else:
            self.pool_empty.add(key)
            self.breaker.note_zero()
        return s0, self.pool.get(key) or []

    def find(self, song, match_fn, extra_kw=True):
        """返回 (candidate|None)。先在歌手池里配, 配不上且 extra_kw 再按 歌手+歌名 单搜一次"""
        if self.breaker.disabled: return None
        s0, pool = self._pool_for(song)
        for c in (pool or []):
            if match_fn(song, c):
                return c
        if not extra_kw or not self.breaker.ready():
            return None
        kw = f'{s0} {song.song_name}' if s0 else song.song_name
        try:
            cands = _search_with_timeout(self.client, self.src, kw, 12) or []
        except Exception:
            cands = []
        time.sleep(self.sleep_s)
        for c in cands:
            if match_fn(song, c):
                self.breaker.note_hit()
                return c
        self.breaker.note_zero()
        return None

def match_kuwo(song, c):
    return bool(c.song_name) and _same_recording(song.song_name, song.singers, c.song_name, c.singers) \
        and isinstance(c.download_url, str) and c.download_url.startswith('http') \
        and getattr(c, 'protocol', '') == 'HTTP'

def match_bili(song, c):
    nname = _norm_str(song.song_name)
    if not nname: return False
    tkey = _norm_str(str(c.song_name or ''))
    ss = [_norm_str(x) for x in (song.singers or '').split('/') if _norm_str(x)]
    return bool(tkey) and nname in tkey and any(s and s in tkey for s in ss) \
        and isinstance(c.download_url, str) and c.download_url.startswith('ytdlp:')

_wx_client = None
def search_wx(song, breaker):
    global _wx_client
    if breaker.disabled: return None
    s0 = str(song.singers or '').split('/')[0].split(',')[0].split('、')[0].strip()
    kw = f'{s0} {song.song_name}' if s0 else song.song_name
    try:
        if _wx_client is None:
            _wx_client = WeixinMusicClient()
        cands = _wx_client.search(keyword=kw) or []
    except WeixinSogouBlocked:
        breaker.disabled = True
        log('[weixin] 搜狗配额触发风控, 本轮熔断')
        return None
    except Exception as e:
        log(f'[weixin] 搜索异常 {type(e).__name__}: {str(e)[:60]}')
        time.sleep(3)
        return None
    time.sleep(1.5)
    for c in cands:
        if not c.song_name: continue
        if _same_recording(song.song_name, song.singers, c.song_name, c.singers) \
           and isinstance(c.download_url, str) and c.download_url.startswith('http'):
            return c
    breaker.note_zero()
    return None

# ---------------- 单曲迁移 ----------------
def apply_replacement(tr, sd, cand, src_name, src_key):
    sd['source'] = src_key
    if getattr(cand, 'identifier', None): sd['identifier'] = cand.identifier
    if isinstance(cand.download_url, str): sd['download_url'] = cand.download_url
    if getattr(cand, 'ext', None): sd['ext'] = str(cand.ext).lstrip('.').lower()
    if getattr(cand, 'file_size', None): sd['file_size'] = cand.file_size
    if getattr(cand, 'file_size_bytes', None): sd['file_size_bytes'] = cand.file_size_bytes
    if getattr(cand, 'duration_s', None):
        sd['duration_s'] = cand.duration_s; sd['duration'] = cand.duration
    tr['source'] = src_name
    tr['ext'] = str(sd.get('ext') or tr.get('ext') or '').lstrip('.').upper()

def resong_track(tr, kuwo, bili, wxb):
    sd = tr.setdefault('_song', {})
    song = SongInfo.fromdict(dict(sd))
    if not _norm_str(song.song_name or '') or not _norm_str(song.singers or ''):
        return 'skip_garbage'
    cand = kuwo.find(song, match_kuwo)
    if cand:
        apply_replacement(tr, sd, cand, 'Kuwo', 'KuwoMusicClient'); return 'kuwo'
    cand = bili.find(song, match_bili)
    if cand:
        apply_replacement(tr, sd, cand, 'Bilibili', 'BilibiliMusicClient'); return 'bili'
    cand = search_wx(song, wxb)
    if cand:
        apply_replacement(tr, sd, cand, 'Weixin', 'WeixinMusicClient'); return 'wx'
    return 'keep_kugou'

def write_group(path, payload, touched_idx):
    fresh = json.loads(path.read_text(encoding='utf-8'))
    for i in touched_idx:
        if i < len(fresh.get('tracks') or []):
            fresh['tracks'][i] = payload['tracks'][i]
    fresh['edited_at'] = int(time.time() * 1000)
    with _lock:
        _atomic_write_json(path, fresh)

def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--groups', default=','.join(DEFAULT_GROUPS))
    args = ap.parse_args()
    groups = args.groups.split(',')

    client = musicdl.MusicClient(
        music_sources=['KuwoMusicClient', 'BilibiliMusicClient'],
        init_music_clients_cfg={
            'KuwoMusicClient': {'work_dir': 'downloads', 'search_size_per_source': 20, 'max_retries': 1, 'disable_print': True},
            'BilibiliMusicClient': {'work_dir': 'downloads', 'search_size_per_source': 20, 'max_retries': 1, 'disable_print': True},
        })
    kuwo = PooledSearch(client, 'KuwoMusicClient', Breaker('kuwo', pause=300, threshold=8, max_pauses=60), 0.5)
    bili = PooledSearch(client, 'BilibiliMusicClient', Breaker('bili', pause=600, threshold=25, max_pauses=10), 0.4)
    wxb = Breaker('weixin', pause=600, threshold=3, max_pauses=0)
    state = load_state()
    log(f'=== 启动迁移: groups={groups} ===')

    for gk in groups:
        info = ETHNIC_BY_KEY.get(gk)
        if not info:
            log(f'未知 group_key: {gk}, 跳过'); continue
        path = _cache_path(gk)
        payload = json.loads(path.read_text(encoding='utf-8'))
        tracks = payload.get('tracks') or []
        targets = [i for i, t in enumerate(tracks)
                   if (t.get('_song') or {}).get('source') == 'KugouMusicClient'
                   and str(i) not in state.get(gk, [])]
        log(f'--- {info["name"]}({gk}): 酷狗源 {sum(1 for t in tracks if (t.get("_song") or {}).get("source")=="KugouMusicClient")} 首, 本轮待处理 {len(targets)} (历史已完成 {len(state.get(gk, []))}) ---')
        gstat = {'kuwo': 0, 'bili': 0, 'wx': 0, 'keep': 0, 'skip_garbage': 0}
        done_ids = state.setdefault(gk, [])
        touched = []
        t0 = time.time()
        for n, i in enumerate(targets):
            tr = tracks[i]
            try:
                r = resong_track(tr, kuwo, bili, wxb)
            except Exception as e:
                log(f'  [{gk}#{i}] 异常 {type(e).__name__}: {str(e)[:70]}')
                r = 'keep_kugou'
            if r in gstat: gstat[r] += 1
            # 断点只记成功/跳过; keep 不记 -> 下轮(酷我恢复后)自动重试
            if r != 'keep_kugou':
                done_ids.append(str(i))
            touched.append(i)
            if len(touched) >= 5:
                write_group(path, payload, touched); touched = []
                save_state(state)
            if (n + 1) % 20 == 0:
                rate = (n + 1) / max(time.time() - t0, 1)
                log(f'  [{info["name"]}] {n+1}/{len(targets)} 累计: 酷我{gstat["kuwo"]} B站{gstat["bili"]} 公众号{gstat["wx"]} 保留{gstat["keep"]} 残渣{gstat["skip_garbage"]} | {rate:.1f}s/首'
                    + (' | [kuwo暂停]' if not kuwo.breaker.ready() and not kuwo.breaker.disabled else '')
                    + (' | [kuwo停用]' if kuwo.breaker.disabled else ''))
        write_group(path, payload, touched)
        save_state(state)
        log(f'=== {info["name"]} 本轮完成: 酷我{gstat["kuwo"]} / B站{gstat["bili"]} / 公众号{gstat["wx"]} / 保留酷狗{gstat["keep"]} / 残渣{gstat["skip_garbage"]} ===')
    log('=== 全部完成 ===')

if __name__ == '__main__':
    main()
