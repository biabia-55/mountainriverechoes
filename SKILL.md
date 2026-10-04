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
- 逐首解析：`SongInfo(source='NeteaseMusicClient', raw_data={'search': tid, ...})` → `client._parsewithofficialapiv1(search_result=tid, song_info_flac=None, lossless_quality_is_sufficient=False)`；**必须只用官方 v1，禁止调 `_parsewiththirdpartapis`**（见下方警告）。
- **歌手页（artist）入库**：网易云歌手页 `music.163.com/#/artist?id=XXXX` 用 `client.post('https://music.163.com/api/v1/artist/{id}')` 直接拿 `hotSongs`（已是完整 song detail，无需像歌单那样先 `trackIds` 展开），逐首 `_parsewithofficialapiv1` 即可。歌手署名以 `artist.name` 或每首 `ar[].name` 为准（注意一字之差：用户给的名 vs 网易云实际署名，如"杨西英子"实为"杨西音子"，按用户字面建卡并提示差异）。
- ⚠️ **第三方解析链恶意域名（已踩坑，必看）**：`NeteaseMusicClient._parsewiththirdpartapis` 的 l1 解析链含 `self._parsewithbileizhenapi`，会访问 `api.bileizhen.top`——该域名被安全中心标记为 **ClearFake 黑灰产**，沙箱直接拒绝连接并 SIGTERM 整个脚本，导致入库中断、零落盘。即使某些歌能在第三方链提前拿到直链而 break，仍有部分歌会走到该域名被拦。**因此网易云入库一律只用 `_parsewithofficialapiv1`**（官方 `interface3.music.163.com`），预览实测 3/3 直链均为官方接口取得，足够。

### 5.1.1 QQ 音乐专辑 / 歌手页入库（qq.py 无 album 接口，需手动拉）
- `QQMusicClient` 只有 `search` / `parseplaylist`（歌单），**没有 album / artist 接口**。要入 QQ 专辑，先手动拉官方专辑 API：
  ```python
  qc = QQMusicClient()
  resp = qc.get('https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg',
                params={'albummid': '001eHuw64QpXgs', 'platform': 'mac', 'format': 'json', 'newsong': '1'})
  songlist = (resp.json().get('data') or {}).get('list') or []
  # 每首项含 songmid / songname / singer[{name}] / albumname / interval(秒)
  for t in songlist:
      si = qc._parsewithofficialapiv1(search_result=t, song_info_flac=None, lossless_quality_is_sufficient=False)
      # 官方 vkey 直链(多为 ogg); 同样禁止 _parsewiththirdpartapis(一堆乱源)
  ```
- QQ 官方 v1 走 `music.vkey.GetVkey`（域名 `u.y.qq.com` / `c.y.qq.com`），安全可用；第三方链（`_parsewiththirdpartapis`）含多个不明第三方直链源，不碰。
- albummid 从专辑页 URL `y.qq.com/n/ryqq_v2/albumDetail/001eHuw64QpXgs` 的路径末段取；歌手页则用 `search` 搜歌手名再取 `songmid` 逐首解析（或直接搜该歌手的专辑 mid）。
- QQ 与网易云 `identifier` 体系不同（QQ 是字母数字 mid，网易云是纯数字 id），跨源按 `(source, identifier)` 去重即可，不会误并。

### 5.1.2 汉族库（GROUP_KEY='han'）的特殊处理（已踩坑）
- 汉族民间小调库 **不在 `ETHNIC_BY_KEY`**（其它民族是 `eNN`，汉族是特殊键 `han`，文件名 `han_汉族民间小调.json`，`_cache_path` 对 `key=='han'` 特判返回该文件）。入库脚本 **不能写 `ETHNIC_BY_KEY['han']`**（KeyError）。
- `_ethnos_payload(info, group_key, songs)` 内部只用 `info['name']`（拼 `name`/`group`）和 `info['kws']`（元数据）两字段，其余自动生成。汉族库入库时从**磁盘现有 payload** 取这俩构造 info：
  ```python
  _base = json.load(open(TARGETS[0], encoding='utf-8'))
  info = {'name': _base.get('group') or (_base.get('name') or '').split(' · ')[0] or '汉族民间小调',
          'kws': _base.get('kws') or []}
  ```
- **同名冲突红线**：汉族库主池可能已有大量 `singers=='西南'` 这类"地域分类标签"曲目（`singers_orig` 为空、非歌手，是建库按地域批量标的）。新建同名歌手卡片前**必须先查主池是否已有同名 `singers`**——否则"清掉同名卡→加新歌"模板会清空数百首主池地域标签。遇到同名：用 `AskUserQuestion` 让用户选（保留主池 / 替换主池 / 改名）；选"保留"时改模板为**不清卡、直接追加、新歌标 `singers_orig`=真实歌手**。
- **DEV/LIVE 同步**：汉族库的 dev 副本与运行 App 实时数据（`~/Library/Application Support/MountainRiverEchoes/...`）偶尔不同步（如有人在 UI 手动建卡只写 live）。入库前先核对两份 `artists_added`/曲目数，把 live 多出部分无害同步回 dev，再双写，保证一致。

### 5.1.3 大批量归类/归并到地域卡片（已踩坑，必看）
- **噪声判定按歌手，不按标题**：标题含 `#话题` / `第X集` / `第X章` / `有声` 的**不一定**是噪声——`17 兰花花#陕北民歌`、`21 采茶灯#福建民歌`、`《拔根芦柴花》二胡同步有声动态简谱`、`安徽花鼓灯男班 第一节` 都是正经民歌，按标题删会大规模误伤（实测 37 首里误伤 27 首）。真正噪声看**歌手账号**：`喜马拉雅 / 小酷说书 / 狮子老爸 / 懒人听书 / 一路听天下 / 金林主播 / 润为有声 / XI_VOICES`。
- **大批量改写前手动额外备份**：`_atomic_write_json` 会归档旧版到 `versions/`，但受 `VERSION_KEEP` 轮转限制，实测批量归并后**归并前那一版没留下**。改写上千首前先手动 `cp` 一份到安全处。
- **单字关键词会误伤**：用省份简称单字（苏/浙/晋/冀/鲁/豫/京/津/闽/粤/湘/鄂/皖/赣/徽/吴/蜀/滇/黔/楚）做地域判定极危险——「**苏**」把「乌**苏**里船歌」（东北）判成江南共 9 首；「花儿」（西北曲种）把「四季**花儿**开」（实为湘鄂民歌）判成西北。优先用**双字以上具体词**。
- **同曲名跟随 vs 省份强规则**：跨地域同曲名（茉莉花、回娘家、十二月望郎）有大量不同省份版本，仅靠"同曲名跟随"会把《回娘家(河北民歌)》拉到湘鄂。精度修正要用**强规则优先**：正则匹配 `省份 + (民歌|小调|山歌|号子|花儿|情歌|船歌)`（如"河北民歌"→华北）覆盖跟随结果；含 `+` 的串烧（兰花花+浏阳河+乌苏里船歌）跳过不判。
- **归并流程模板**：① 备份 ② 剔除真噪声 ③ 关键词判定 ④ 同曲名迭代跟随（把命中曲名回灌映射再跑 1-2 轮） ⑤ 剩余按用户指定兜底卡 ⑥ 省份强规则精度修正 ⑦ 双写 + 校验"各卡之和 == 总数"且"非目标卡残留为空"。

### 5.1.4 ⚠️ `_ethnos_payload` 会用 `_song.singers` 覆盖人工标签（最高危踩坑，已实际酿成回归）
- **现象**：入库脚本习惯写成 `payload = mre._ethnos_payload(info, KEY, all_songs)`，其中 `all_songs = [SongInfo.fromdict(t['_song']) for t in existing] + new_songs`。重建时 track 的 `singers` **取自 `SongInfo.singers`（即 `_song` 里的真实歌手名）**，会**静默覆盖**此前人工设到 `track['singers']` 的卡片/地域标签。
- **实际事故**：汉族库 8 卡大归并（2440 首全部贴好地域标签）后，再跑一次普通入库，`_ethnos_payload` 重建把归并结果**全部抹回真实歌手名**——西南 449→292、湘鄂 245→174、非 8 卡残留 0→561，归并工程白做。靠手动备份才救回。
- **根因**：归并/贴标签只改了 `track['singers']`，**没改 `_song['singers']`**；一旦 payload 重建就从 `_song` 重新取值。
- **正确做法（三选一，按安全性递增）**：
  1. **最稳**：不整份重建——读现有 JSON，只在 `d['tracks']` 上 `append` 新 track，人工改元数据字段（count 等），**绝不调用 `_ethnos_payload` 重建已贴标签的库**。
  2. 必须重建时，重建后**按 `identifier`/`(song_name, identifier)` 从备份映射逐条还原** `singers` / `singers_orig`（脚本见 `/tmp/fix_han_singers_restore.py`）。
  3. 贴标签时**同步写 `_song['singers']`**，让重建也能取到卡名（会污染 `_song` 原始字段，慎用）。
- **红线**：对**已做过归并/贴卡片的库**（尤其是汉族库 han 这种按地域贴过标签的）做追加入库前，**必须先手动 `cp` 备份**，且入库后立刻核对"各卡之和 == 总数"与"非目标卡残留 == 0"。


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

### 5.3 微信文章/专辑入库（wx_video_album.py + 桥接 eXX）

微信源已接入默认音源（`WeixinMusicClient`），但搜狗 fallback 常触发 `WeixinSogouBlocked` 限流、不可靠；正文直连有时返 `ret=-2` 验证页，需多试/换篇。解析用项目根 `wx_video_album.py`（直连 mp.weixin.qq.com，绕开 musicdl 标准 API）：
- 单篇：`--url "https://mp.weixin.qq.com/s/xxx" --out one.json`
- 合集：`--album-url "https://mp.weixin.qq.com/mp/appmsgalbum?__biz=Y&action=getalbum&album_id=Z" --out album.json`（自动拆 __biz+album_id，paging 翻页拿全）
- 输出统一 schema 条目：`kind:'audio'`（mpvoice，含 `voice_id`=mediaid + `download_url`=getvoice 直链）/`kind:'video'`（mpvideo/qqvid，短效直链）

⚠️ 踩坑：`parse_voices` 正则只认 `title="..."singer_name="..."` 结构，会漏掉大量用 `name="..."play_length="..."` 且无 title 的文章（如"羌族歌曲欣赏"系列，body 含 20 个 voice_encode_fileid 只抽到 5）；且微信内联 JSON 用 `\x22` 转义引号。→ 在桥接脚本里 **monkeypatch 健壮版 `parse_voices`**（`body.replace('\\x22','"').replace('\\x26','&')` 后匹配 `<mpvoice ...>` 标签取 voice_encode_fileid/name/play_length），**不动项目源码**即可多抽数倍音频。

⚠️ 实战补充（2026-10-04 道真仡佬篇）：上面那版健壮 `parse_voices` 还要再扩三点，否则「道真公共文化」这类**新版音频卡片会 0 命中**（单篇 3.4MB 正文里 30 个 `voice_encode_fileid` 却抽不到）：
  - 标签形态不止 `<mpvoice ...>`，还有 **`<mp-common-mpaudio ...>`**（2024+ 公众号"音频卡片"用这个），必须两种都 `re.finditer` 匹配；
  - 真实歌名**不在 `name=` 属性**（新版里 `name="insertaudio"` 恒为插件类名，不是歌名），而是藏在 `src="/cgi-bin/readtemplate?t=tmpl/audio_tmpl&name=<URL编码歌名>&play_length=<URL编码时长>"` 的 `name=` 查询参数里 → 必须用 `urllib.parse.unquote` 解出（例 `%E7%88%B1%E4%B8%8A%E4%BB%A1%E5%B1%B1` →「爱上仡山」）；
  - 歌手在 `author=` 属性（如 `author="道真公共文化"`），`singer_name` 多数为空；时长在 `play_length="241000"`（毫秒，/1000）。
  健壮正则：`re.finditer(r'<(mpvoice|mp-common-mpaudio)\\b([^>]*)>', body)`，mediaid 取 `voice_encode_fileid`；歌名优先取 `src` 里 URL 编码的 `name=`，否则回退 `name=`/`title=`；歌手取 `author=`，否则 `singer_name=`；时长 `play_length`/1000。完整实战实现见 `/tmp/ingest_wx_e35.py`（道真仡佬篇）。

桥接成 eXX track（与网易云同理，见 5.2）：`source:'Weixin'`、`source_cn:'微信公众号'`；音频 `_song.download_url='https://res.wx.qq.com/voice/getvoice?mediaid={voice_id}'`（**免鉴权永久直链**，实测 HTTP 200 audio/mp3 可播）、`_song.identifier=voice_id`(mediaid)；视频 `_song.download_url`=mpvideo 直链（**签名时效约 2 天**，过期由播放侧回源重取），视频 voice_id 空时用 `wxvid-{article_msgid}-{vid}` fallback 保唯一。**自愈线索**写 `_song.raw_data.wechat={'voice_id':voice_id,'url':article_url,'article':''}`（音频一般不用，视频必过期需它回文章页重抓 mpvideo）。歌手卡：`singers='卡片名'`、原歌手存 `singers_orig`、`artists_added` 登记；`album` 若被微信 HTML 的 `nickname:"data-miniprogram-nickname"` 污染则回退卡片名。

双写纪律（同网易云）：**dev + live**（`~/Library/Application Support/MountainRiverEchoes/webui/ethnos_cache/eXX_族.json`）—— 运行中的 8766 app 读 live，网页验收前先确认落 live。

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
