---
name: mountainriverechoes-ingest
description: 唤醒词「将这个链接入“某某”民族库」。把一个来自酷狗、酷我、咪咕、网易云音乐、QQ音乐、B站或微信公众号的链接（单首歌曲、歌手页、歌单/电台、合集/收藏夹、公众号文章/合集/视频专辑）解析成可播放音源并写入对应的中国 56 民族音乐曲库。Use when the user says 将这个链接入“某某”民族库 / 把这个歌单入某族库 / 把这篇微信文章入某族库, i.e. gives a music-platform URL and wants it ingested into an ethnic-group library of the 山河回响 (mountainriverechoes) project.
version: 0.4.0
metadata:
  requires:
    anyBins:
      - python3
placeholders:
  # 仓库根目录; 用法: 把下文所有 {PROJECT_ROOT} 替换成你 clone 仓库的绝对路径。
  # 例: PROJECT_ROOT="/Users/you/GTCODE/musicdl copy"
  PROJECT_ROOT: "<absolute path to your clone of the musicdl copy repo>"
----------------------------------------------------------------------

# 山河回响 (mountainriverechoes) 56 民族曲库入库（按 URL）

> **路径占位符**：本 skill 把仓库根目录写作 `{PROJECT_ROOT}`。
> 实际使用前，请把全文所有 `{PROJECT_ROOT}` 替换为你本地 clone 这个仓库的绝对路径（路径里有空格时用引号包起来，如 `cd "{PROJECT_ROOT}"`）。

## 0. 唤醒词与触发

**唤醒词：`将这个链接入“某某”民族库`**（以及等价说法：「把这个歌单入佤族库」「这篇微信文章入景颇族」等）。

解析规则：

- 「某某」= 民族名（如 景颇族 / 佤族 / 傈僳族 …）→ 对应 `group_key = eXX`（对照表见 `{PROJECT_ROOT}/webui/mountainriverechoes.py` 中 `ETHNIC_GROUPS`，`e00_蒙古族` 到 `e54_基诺族`）；「汉族」→ `group_key = 'han'`（汉族民间小调库）。
- 拿不准 group_key 时：`python -c "import sys; sys.path.insert(0,'{PROJECT_ROOT}'); from webui.mountainriverechoes import ETHNIC_BY_KEY; print({k: v['name'] for k,v in ETHNIC_BY_KEY.items()})"`。
- **调用者（或用户原话）必须指明入哪个族** —— 本 skill 不做民族归属闸门判断。
- 已在库里 → 如实告知并询问是否补充新音源，不重复入库（判定见 §1）。

工作目录：`{PROJECT_ROOT}`（Flask 后端 `{PROJECT_ROOT}/webui/mountainriverechoes.py` 在 8766 端口常驻；入库走直写函数，不依赖服务进程）。

## 1. 入库决策流程

```
1. 抓取/解析出 SongInfo 列表(name / singers / download_url / ext)
        ↓
2. 逐条在目标族库查重: 复用主程序 _dedup_key(song)（归一化歌名+歌手口径）
   命中 → 老条目保留(warm 优先), 新条目跳过
        ↓
3. 合并写盘: _ethnos_payload(info, group_key, songs) + _atomic_write_json(path)
   （自动原子写 + 归档上一版到 webui/ethnos_cache/versions/）
        ↓
4. 提示: 已存在 N 条 / 新增 M 条 / 现有总计 T 条
```

**参考实现**：`{PROJECT_ROOT}/import_bili_favlist.py` 的 `merge_into()` 是实战验证过的标准写法
（warm 1000 分 > 新增 800 分排序、`_dedup_key` 去重、`_atomic_write_json` 落盘）——直接照抄它的模式，不要另起炉灶。

### 1.1 判定/写库函数（直接 import 主程序的）

```python
import sys, json, threading
sys.path.insert(0, '{PROJECT_ROOT}')
from webui.mountainriverechoes import (
    _dedup_key, _ethnos_payload, _atomic_write_json, _cache_path,
    ETHNIC_BY_KEY, ETHNOS_SCHEMA, _same_recording,
)
from musicdl.modules import SongInfo

_lock = threading.Lock()

def exists_in_ethnicity(name, singers, group_key):
    """按 _same_recording 口径查重(归一化歌名完全相等 + 歌手相容)"""
    path = _cache_path(group_key)
    if not path.exists():
        return (False, None)
    payload = json.loads(path.read_text(encoding='utf-8'))
    for t in payload.get('tracks') or []:
        s = SongInfo.fromdict(t.get('_song') or {})
        if _same_recording(name, singers, s.song_name, s.singers):
            return (True, s)
    return (False, None)

def merge_into(group_key, new_songs):
    """new_songs: List[SongInfo]（不是 dict!）。warm 优先合并 + 原子落盘。"""
    path = _cache_path(group_key)
    info = ETHNIC_BY_KEY[group_key]
    merged, warm_keys = {}, set()
    if path.exists():
        d = json.loads(path.read_text(encoding='utf-8'))
        if d.get('v') == ETHNOS_SCHEMA:
            for t in d.get('tracks') or []:
                s = SongInfo.fromdict(t.get('_song') or {})
                if s.song_name:
                    k = _dedup_key(s)
                    merged[k] = (s, 1000)   # 老条目 warm 优先
                    warm_keys.add(k)
    added = 0
    for s in new_songs:
        if s is None: continue
        k = _dedup_key(s)
        if k in warm_keys: continue
        merged[k] = (s, 800)
        warm_keys.add(k)
        added += 1
    songs = [v[0] for v in sorted(merged.values(), key=lambda x: -x[1])]
    with _lock:
        _atomic_write_json(path, _ethnos_payload(info, group_key, songs, partial=False))
    return added, len(songs)
```

**注意**：`_dedup_key(song)` / `song_brief(song, source, idx)` 接收的是 **SongInfo 对象**（带属性），
dict 必须先 `SongInfo.fromdict(d)` 转换，直接传 dict 会 AttributeError。

### 1.2 合集分流

合集（歌单 / 电台 / 公众号合集 / B 站收藏夹）解析出的每条曲目**独立**走查重与合并；
歌手卡片按每条的 `singers` 字段归组——同一合集里多个歌手的歌会落到各自歌手名下（不是全塞进合集名卡片）。

## 2. 工具栈速览

| 入口形态 | 工具 | 说明 |
| --- | --- | --- |
| 微信公众号文章 URL | `wx_video_album.py --url` | mpvoice 音频 + mpvideo 视频 + 腾讯视频兜底 |
| 微信公众号合集 URL | `wx_video_album.py --album-url` | 自动拆 `__biz` + `album_id` 翻页 |
| 微信公众号视频专辑 | `wx_video_album.py --biz --album` | 同上 |
| 微信文章列表批量 | `wx_video_album.py --articles-file` | JSON 驱动，`--resume` 断点续抓 |
| 网易云/酷狗/酷我/咪咕/QQ/B站 | `musicdl.MusicClient` 各源 `search()` / `_parse*` | 六大源 |
| B 站收藏夹 | `import_bili_favlist.py --favlist --ethnicity eXX` | 内置 favlist→族 映射可扩展 |

> 搜索侧（WebUI 在线搜索）现在默认七源（含微信公众号），但**入库通道**里微信仍走 `wx_video_album.py`
> （它支持 mpvideo 的会话绑定直链，检索客户端拿不到这条链路）。

**写盘纪律**：直接 `import` 主程序的 `_ethnos_payload / _dedup_key / _atomic_write_json`，
**不要**新开 `tools_*_ingest.py` 之类的副本工具——历史上就是因为另一份 `_dedup_key` 实现漂移出过死链。
也可以走主程序 `/api/ethnos/ingest`，但调用方沙箱内启动的进程会被杀，**生产路径用直写**。

## 3. URL 形态识别

```
1. mp.weixin.qq.com/s?...                   单篇文章     → §4-A (--url)
2. mp.weixin.qq.com/mp/appmsgalbum?         公众号合集   → §4-B (--album-url)
3. music.163.com                            网易云       → §5 (musicdl Netease)
4. kugou.com                                酷狗         → §5 (musicdl Kugou)
5. kuwo.cn                                  酷我         → §5 (musicdl Kuwo)
6. migu.cn                                  咪咕         → §5 (musicdl Migu)
7. y.qq.com                                 QQ音乐       → §5 (musicdl QQ)
8. bilibili.com/video/BVxxx 等              B站单曲/合集 → §5 (musicdl Bilibili)
9. bilibili.com 收藏夹(space.bilibili.com/.../favlist)   → §6 (import_bili_favlist)
```

## 4. 微信公众号（wx_video_album.py）

### 4-A. 单篇文章

```bash
cd "{PROJECT_ROOT}"
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
  ./venv/bin/python wx_video_album.py \
    --url "https://mp.weixin.qq.com/s?__biz=...&mid=...&idx=1&sn=..." \
    --out /tmp/wx_one.json --limit 3 --workers 1 --sleep 1.5
```

`--limit 3` 先试 3 篇防抓空。

### 4-B. 合集 URL

```bash
cd "{PROJECT_ROOT}"
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
  ./venv/bin/python wx_video_album.py \
    --album-url "https://mp.weixin.qq.com/mp/appmsgalbum?__biz=MzIyNzQ1ODM5OQ==&action=getalbum&album_id=3245491316105527302#wechat_redirect" \
    --out /tmp/wx_album.json --limit 3 --workers 1 --sleep 1.5
```

**反爬提示**：2024 起公众号 server 对没带 `appmsg_token` 的请求返 `ret=-2` 空壳（body < 60KB、不含
mpvoice/mpvideo）。`harvest_article` 会自动转搜狗反向搜索兜底（mpvoice 直链能拿到，mpvideo/qqvid 拿不到）。
已被反爬的合集短期内重跑只会 0 命中，不要反复浪费。

### 4-C. 输出 → 写库

`wx_video_album.py` 输出的统一 schema 条目，先转 `SongInfo` 再走 §1.1 的 `merge_into()`：

```python
from musicdl.modules import SongInfo
raw = json.load(open('/tmp/wx_one.json', encoding='utf-8'))
songs = [SongInfo.fromdict(it) for it in raw if isinstance(it, dict) and str(it.get('download_url') or '').startswith(('http',))]
added, total = merge_into(group_key, songs)
print(f'新增 {added} 条, 库内总计 {total}')
```

## 5. 网易云/酷狗/酷我/咪咕/QQ/B站（musicdl 六源）

```python
import sys
sys.path.insert(0, '{PROJECT_ROOT}')
from musicdl import musicdl

client = musicdl.MusicClient(
    music_sources=['NeteaseMusicClient'],   # 按 URL 域名挑源
    init_music_clients_cfg={'NeteaseMusicClient':
        {'work_dir': 'downloads', 'search_size_per_source': 15, 'max_retries': 1}},
)
songs = client.music_clients['NeteaseMusicClient'].search(
    keyword='景颇族民歌', num_threadings=2, request_overrides={}, rule={},
)
# songs: List[SongInfo] —— 直接走 §1.1 merge_into(group_key, songs)
```

**注意**：

- 网易云电台/DJ 音频直链约 40 分钟时效——入库后播放时由 `_netease_reissue_url` 按 identifier 自动续签，入库侧无需预处理。
- 酷我直链签名数天过期——播放时由 `_kuwo_reissue_url` 按库内 rid 直签（搜索接口搜不出来的小众曲目也必中）。
- QQ 音乐 `/song?mid=xxx` 链接：入库后由 `_qqmusic_reissue_url` 按 mid 续签。
- B 站单曲解析用 `BilibiliMusicClient()._parsewithofficialapiv1(...)`（参考 `import_bili_favlist.py` 的 `parse_one()`，每次 fresh 客户端规避单例损坏）。

### 5.1 网易云歌单 id 提取（坑）
- `musicdl` 的 `NeteaseMusicClient().parseplaylist(url)` 内部先 `session.head(url)`，会把 `music.163.com/#/playlist?id=XXXX` 的 **fragment 丢掉**，导致取不到 id、返回空列表。
- 正确做法（自己取 id，别调 `parseplaylist`）：
  ```python
  from urllib.parse import urlparse, parse_qs
  pid = parse_qs(urlparse(url).fragment).get('id', [None])[0]   # '7111745461'
  resp = client.post('https://music.163.com/api/v6/playlist/detail', data={'id': pid})
  track_ids = (resp.json()['playlist']['trackIds'])             # 逐首 _parsewithofficialapiv1 解析
  ```
- 逐首解析：`SongInfo(source='NeteaseMusicClient', raw_data={'search': tid, ...})` → `client._parsewithofficialapiv1(search_result=tid, lossless_quality_is_sufficient=False)`；`_parsewiththirdpartapis` 可省（官方直链够用）。

### 5.2 在民族库内建"歌手卡片"（正确位置，别写进全局"我的歌单"）
- ⚠️ 民族库**内部**的卡片 = **歌手卡片**，由该族缓存文件 `webui/ethnos_cache/eXX_族.json` 里每条曲目的 **`singers` 字段**扫描聚合而成（见 3 节）。它出现在「民族库二级页 → 歌手目录」，**不在**左侧全局"我的歌单"。
- 全局"我的歌单"是 `ui_state.json` 的 `cm_my_playlists`，是独立于民族库的侧栏卡片——用户明确不要歌单落在这里，别用。
- 用户说「在某某库新建《某某》歌手卡片，把这批歌放进去」→ 做法：
  1. 这批歌先进主池（见 5 节 fetch+merge，带 `identifier`/`download_url` 可续签播放）；
  2. 把这**批曲目的 `singers` 改为卡片名**（如 `'仡佬侗苗歌'`）——这就是建卡本身，索引按 `singers` 自动聚出该歌手卡片；
  3. 同时把卡片名登记进该族缓存的 `artists_added`（确保可见），并从 `artists_removed` 解禁；
  4. 点该卡片 → 虚拟歌单 `artist:卡片名@族名`，按 `singers` 过滤回池取曲，可整批播放。
- 回滚保护：改 `singers` 前把原歌手名存到 **`singers_orig`** 字段（UI 不读，纯备份），要恢复真实歌手时回填即可。
- 索引按文件 mtime 签名刷新（`_ethnos_files_sig`），改盘后刷新网页即生效，**无需重启服务**。
- 精度提示：进主池会稀释目标族精度（例：侗苗歌单全量入仡佬族池，0 首真仡佬）。如要"只留卡片、清主池"，回滚时把主池里 `source=NeteaseMusicClient` 的这批整删即可。

## 6. B 站收藏夹

```bash
cd "{PROJECT_ROOT}"
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
  ./venv/bin/python import_bili_favlist.py \
    --favlist "https://space.bilibili.com/xxx/favlist?fid=..." \
    --ethnicity e52 \
    --out /tmp/bili_e52.json
```

内置映射见脚本内 `FAVLIST_MAP`（如 4133785249→e52 门巴、4144803649→e53 珞巴），新收藏夹往那里加一行。

## 7. 播放侧自愈（入库后你需要知道的事）

直链天然带时效，过期由 WebUI 播放时的**四层自愈链**处理，入库时无需预处理：

```
① 库内直链(仍有效则直接播)
   ↓ 失效
② 按 id 续签: 酷我 _kuwo_reissue_url(rid) / 网易云 _netease_reissue_url(id)
   / 微信 _refresh_weixin_link(回文章页) / QQ _qqmusic_reissue_url(mid)
   ↓ 不适用
③ B 站实时解析(ytdlp: 形态, 永不过期, 自愈候选优先于四源重搜)
   ↓ 未命中
④ 四源精确同名重搜(Kugou/Kuwo/Migu/Netease)
   ↓ 命中后 _persist_song_link 把新直链回写曲库文件(含 versions/ 单版快照)
```

因此 **identifier 字段是续签的生命线**：入库时务必保留各源的原始 id（酷我 rid / 网易云 song id / QQ mid / B 站 avid）。

## 8. 调用约束（避免踩坑）

1. **必须显式指定民族**：唤醒词里的「某某」决定 `group_key`；不做民族相关性判断。
2. **必须干净环境变量**：所有外部抓取命令加 `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy` 前缀，否则撞 127.0.0.1:7897 死代理，外部请求全挂。
3. **写盘直写**：沙箱内启动的进程会被杀，`/api/ethnos/ingest` 不可用；直接 import 主程序函数落盘，**不要**另起 `tools_*_ingest.py` 副本。
4. **先查重再写**：走 §1.1 `merge_into()`（内置 `_dedup_key` 去重 + warm 优先），一篇文章多 mpvoice 也不会重复入库。
5. **已入库的微信合集别重抓**：重跑只会 0 命中（ret=-2 风控或库内已存），属于浪费。
6. **曲库已入 git**：`webui/ethnos_cache/` 主数据在版本控制内，每次写盘 git 历史自动留档（应用自身还会在 `versions/` 留上一版快照）；误删可 `git checkout` 找回。

## 9. 失败处理

| 现象 | 原因 | 解法 |
| --- | --- | --- |
| 微信文章抓回 body < 60KB | ret=-2 反爬（2024+ 新版公众号） | `harvest_article` 已自动转搜狗兜底；仍 0 则标记「暂无法重抓」 |
| 合集跑完 0 条 audio/video | 合集本身是图文歌谱类 | 跟用户确认；不强求 |
| 网易云电台直链 403 | 40 分钟 vkey 时效 | 不重抓；播放时 `_netease_reissue_url` 按 identifier 续签 |
| musicdl 某源 0 命中 | 关键词太精准或源站限流（当日高频搜索易触发） | 换关键词 / 换源 / 稍后再试；酷我酷狗限流隔天恢复 |
| `AttributeError: 'dict' object has no attribute ...` | 把 dict 传给了要 SongInfo 的函数 | `SongInfo.fromdict(d)` 先转换（见 §1.1 注意） |
