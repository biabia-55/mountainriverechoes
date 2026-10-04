#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""景颇库逐歌手酷狗迁移: 按歌手(曲目数降序)逐一排查酷狗收录, 酷我→B站→(微信熔断则跳过)替换。

断点: /tmp/resong_singer_state.json 记录已完成歌手; keep 的曲目不计断点(下轮重试)。
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections import Counter
from webui.mountainriverechoes import _cache_path, ETHNIC_BY_KEY
from musicdl import musicdl
from musicdl.modules import SongInfo
import resong_kugou as RK
from resong_kugou import PooledSearch, Breaker, match_kuwo, match_bili

GROUP = 'e26'
STATE_FILE = '/tmp/resong_singer_state.json'
log = RK.log

def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {'singers_done': []}

def main():
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
    done_clean = set(state['singers_done'])   # 持久化: 本轮已"干净"的歌手(无保留曲目)
    log(f'=== 景颇库逐歌手迁移启动 (历史已完成 {len(done_clean)} 位) ===')
    MAX_PASSES = 12
    for pass_no in range(1, MAX_PASSES + 1):
        path = _cache_path(GROUP)
        payload = json.loads(path.read_text(encoding='utf-8'))
        tracks = payload.get('tracks') or []
        groups = {}
        for i, t in enumerate(tracks):
            if (t.get('_song') or {}).get('source') != 'KugouMusicClient':
                continue
            s = str(t.get('singers') or '').strip()
            if s:
                groups.setdefault(s, []).append(i)
        worklist = sorted(((s, idxs) for s, idxs in groups.items() if s not in done_clean),
                          key=lambda x: -len(x[1]))
        if not worklist:
            log('=== 景颇库: 所有歌手已排查完毕 ==='); break
        info = ETHNIC_BY_KEY[GROUP]
        replaced_total = 0
        info = ETHNIC_BY_KEY[GROUP]
        for singer, idxs in worklist:
            stat = {'kuwo': 0, 'bili': 0, 'wx': 0, 'keep': 0}
            for i in idxs:
                tr = tracks[i]
                sd = tr.setdefault('_song', {})
                song = SongInfo.fromdict(dict(sd))
                r = 'keep'
                try:
                    cand = kuwo.find(song, match_kuwo)
                    if cand:
                        RK.apply_replacement(tr, sd, cand, 'Kuwo', 'KuwoMusicClient'); r = 'kuwo'
                    else:
                        cand = bili.find(song, match_bili)
                        if cand:
                            RK.apply_replacement(tr, sd, cand, 'Bilibili', 'BilibiliMusicClient'); r = 'bili'
                        else:
                            cand = RK.search_wx(song, wxb)
                            if cand:
                                RK.apply_replacement(tr, sd, cand, 'Weixin', 'WeixinMusicClient'); r = 'wx'
                except Exception as e:
                    log(f'  [{singer}] #{i} 异常 {type(e).__name__}: {str(e)[:60]}')
                stat[r if r in stat else 'keep'] += 1
            # 写盘(重读合并)
            fresh = json.loads(path.read_text(encoding='utf-8'))
            for i in idxs:
                if i < len(fresh.get('tracks') or []):
                    fresh['tracks'][i] = tracks[i]
            fresh['edited_at'] = int(time.time() * 1000)
            RK._atomic_write_json(path, fresh)
            replaced_total += stat['kuwo'] + stat['bili'] + stat['wx']
            # 只有"零保留"的歌手才计入持久化完成; 有保留的下轮重试
            if stat['keep'] == 0:
                done_clean.add(singer)
                state['singers_done'] = sorted(done_clean)
                json.dump(state, open(STATE_FILE, 'w'))
            log(f'[pass{pass_no}][{singer}] 酷狗 {len(idxs)} 首 → 酷我{stat["kuwo"]} B站{stat["bili"]} 公众号{stat["wx"]} 保留{stat["keep"]}'
                + (' | [kuwo暂停]' if not kuwo.breaker.ready() and not kuwo.breaker.disabled else '')
                + (' | [kuwo停用]' if kuwo.breaker.disabled else ''))
            time.sleep(0.5)
        log(f'--- 第 {pass_no} 轮完成: 替换 {replaced_total} 首 ---')
        if replaced_total == 0:
            log('本轮零替换(源限流/无替身), 退出; 稍后重跑即可续(保留曲目会自动重试)')
            break
    log('=== 迁移驱动退出 ===')

if __name__ == '__main__':
    main()
