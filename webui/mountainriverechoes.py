'''
Function:
    musicdl 民族音乐 Web 界面 (后端)
    启动后访问 http://127.0.0.1:8766
    下载产物统一写入仓库根目录 downloads/
'''
import os
import re
import sys
import json
import time
import uuid
import shutil
import mimetypes
import subprocess
import threading
from pathlib import Path
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import requests
from flask import Flask, jsonify, request, render_template, send_from_directory, Response

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))   # 仓库根(musicdl 所在), 免启动时依赖 PYTHONPATH

from musicdl import musicdl
from musicdl.modules import MusicClientBuilder, LyricSearchClient

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = BASE_DIR / 'downloads'
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

HOST, PORT = '127.0.0.1', 8766
# 域名 → 客户端 source (用于 /api/playlist 慢速解析时按 URL 选源)
HOST_HINTS = [('kugou', 'KugouMusicClient'), ('kuwo', 'KuwoMusicClient'), ('y.qq.com', 'QQMusicClient'), ('migu', 'MiguMusicClient'), ('qishui', 'SodaMusicClient'), ('douyin', 'SodaMusicClient')]
# CDN 防盗链 Referer: 按直链 URL 域名匹配, 给出对应官方域名, 缺了必 403。
# Kugou sharefs/fspc.kugou.com / Kuwo kw-er.kuwo.cn / Migu sc.migudm.com / QQ isure.stream.qqmusic.qq.com / 微信 mpvideo / 网易云 / B站 都验证过
RETRY_REFERERS = [
    ('kugou.com',       'https://www.kugou.com/'),
    ('kuwo.cn',         'https://www.kuwo.cn/'),
    ('migudm.com',      'https://migu.cn/'),
    ('migu.cn',         'https://migu.cn/'),
    ('stream.qqmusic',  'https://y.qq.com/'),
    ('isure.stream',    'https://y.qq.com/'),
    ('y.qq.com',        'https://y.qq.com/'),
    ('gtimg.com',       'https://v.qq.com/'),
    ('v.qq.com',        'https://v.qq.com/'),
    ('bilibili.com',    'https://www.bilibili.com/'),
    ('bilivideo.com',   'https://www.bilibili.com/'),
    ('music.126.net',   'https://music.163.com/'),
    ('music.163.com',   'https://music.163.com/'),
    ('res.wx.qq.com',   'https://mp.weixin.qq.com/'),
    ('mpvideo.qpic.cn', 'https://mp.weixin.qq.com/'),
]
def _referer_for(url):
    """按 URL 域名找 Referer; 没匹配返 None 让转发继续走默认头。"""
    if not isinstance(url, str):
        return None
    for dom, ref in RETRY_REFERERS:
        if dom in url:
            return ref
    return None
# 默认音源(2026-09-29 实测, 关键词「茉莉花」「山歌」/每源 6 条):
#   Bilibili 6-7首/3.7-4.5s  ← 最快; 民族现场与原生态内容最丰富, 故排首位
#   Weixin   4首真实MP3/8-20s ← 受搜狗 IP 级配额限制(公众号文章独家索引), 超限返回验证页, 轻度 45s 自愈
#   Migu     20首/快
#   Kugou    6首flac/25s     ← 旧注释"卡死永不 done"是多源串行测试的假象, 单独实测正常出 6 首
#   Netease  6首flac/78.5s
#   Kuwo     18首/27s
# 已剔除: MyFreeMP3(源已死, 返回 0 首) / QQ(105s 仅 1 首) / TwoT58(偶发挂起) /
#         YouTube(8 个第三方转换站聚合器, 与 yt-dlp 播放通道无关, 不可靠)
# 搜索逐源流式返回: 快源先出结果, 慢源只影响最后补结果, 不拖慢首屏。
#
# 只有一套音源, 与原版 musicdl 一致 —— 下载按 song_info.source 回落到该源自己的客户端,
# 架构上不可能存在独立于搜索的"下载源"。此前 UI 上的第二组勾选框是给不存在的概念做的界面。
DEFAULT_SOURCES = ['BilibiliMusicClient', 'MiguMusicClient', 'NeteaseMusicClient',
                   'KugouMusicClient', 'KuwoMusicClient', 'QQMusicClient', 'WeixinMusicClient']
# WeixinMusicClient 搜索通道(搜狗微信 type=2 -> 解析正文 mpvoice/腾讯视频) 2026-10-03 起接入默认音源:
# 曲库构建期已结束, 以后只手动逐次搜索 —— 搜狗 IP 级配额按次消耗完全可控。
# 注意搜狗超限时抛 WeixinSogouBlocked, 前端在「微信公众号」chip 上显示红色 "!" 属正常配额表现;
# 大规模遍历(ETHNOS_SOURCES 构建等)仍不得携带 Weixin, 避免批量烧配额。
FAST_SOURCES = ['Bilibili', 'Migu', 'XMFWAV', 'Sgogo', 'Kuwo', 'Gequhai']
SEARCH_SIZE_PER_SOURCE = 12
AUDIO_EXTS = {'.mp3', '.flac', '.wav', '.m4a', '.ape', '.ogg', '.oga', '.wma', '.aac', '.opus'}
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36'

# 网易云官方榜单 (歌单ID长期稳定)
NETEASE_CHARTS = [
    {'id': '3778678',  'name': '热歌榜',   'desc': '云音乐热歌榜', 'grad': ['#ff5f6d', '#ffc371']},
    {'id': '3779629',  'name': '新歌榜',   'desc': '云音乐新歌榜', 'grad': ['#36d1dc', '#5b86e5']},
    {'id': '19723756', 'name': '飙升榜',   'desc': '云音乐飙升榜', 'grad': ['#f7971e', '#ffd200']},
    {'id': '2884035',  'name': '原创榜',   'desc': '云音乐原创榜', 'grad': ['#834d9b', '#d04ed6']},
    {'id': '71385702', 'name': 'ACG音乐榜', 'desc': '云音乐ACG音乐榜', 'grad': ['#654ea3', '#eaafc8']},
    {'id': '991319590', 'name': '说唱榜',  'desc': '云音乐说唱榜', 'grad': ['#0f2027', '#2c5364']},
]
# QQ音乐巅峰榜 (topid 长期稳定)
QQ_CHARTS = [
    {'id': '26', 'kind': 'toplist', 'name': '热歌榜', 'desc': '巅峰榜·热歌', 'grad': ['#f5515f', '#9f041b']},
    {'id': '27', 'kind': 'toplist', 'name': '新歌榜', 'desc': '巅峰榜·新歌', 'grad': ['#00b4db', '#0083b0']},
    {'id': '4',  'kind': 'toplist', 'name': '流行指数榜', 'desc': '巅峰榜·流行指数', 'grad': ['#f7971e', '#fd9853']},
    {'id': '62', 'kind': 'toplist', 'name': '巅峰飙升榜', 'desc': '巅峰榜·飙升', 'grad': ['#7f00ff', '#e100ff']},
]
# 汽水音乐精选歌单 (playlist_id 来自公开分享链接) —— 「安静曲」已于 2026-10-05 应用户要求移除
SODA_PRESETS = []
PRESET_PLAYLISTS = [
    {'platform': 'netease', 'platform_name': '网易云音乐', 'color': '#d33a31', 'items': NETEASE_CHARTS},
    {'platform': 'qq', 'platform_name': 'QQ音乐', 'color': '#31c27c', 'items': QQ_CHARTS},
    {'platform': 'soda', 'platform_name': '汽水音乐', 'color': '#ff5252', 'items': SODA_PRESETS},
]

# ---------------------------------------------------------------- 民族音乐(55个少数民族)
ETHNIC_BASE = ['蒙古族', '回族', '藏族', '维吾尔族', '苗族', '彝族', '壮族', '布依族', '朝鲜族', '满族',
               '侗族', '瑶族', '白族', '土家族', '哈尼族', '哈萨克族', '傣族', '黎族', '傈僳族', '佤族',
               '畲族', '高山族', '拉祜族', '水族', '东乡族', '纳西族', '景颇族', '柯尔克孜族', '土族', '达斡尔族',
               '仫佬族', '羌族', '布朗族', '撒拉族', '毛南族', '仡佬族', '锡伯族', '阿昌族', '普米族', '塔吉克族',
               '怒族', '乌孜别克族', '俄罗斯族', '鄂温克族', '德昂族', '保安族', '裕固族', '京族', '塔塔尔族', '独龙族',
               '鄂伦春族', '赫哲族', '门巴族', '珞巴族', '基诺族']
ETHNIC_EXTRA_KW = {
    '蒙古族': ['马头琴', '呼麦', 'ᠳᠠᠭᠤᠤ'], '藏族': ['藏语歌曲', 'གླུ་གཞས', 'དམངས་གླུ'], '维吾尔族': ['木卡姆', 'ناخشا', 'مۇقام'], '苗族': ['苗岭飞歌', 'nkauj hmoob', 'kwv txhiaj'],
    '朝鲜族': ['阿里郎', '연변 민요'], '傣族': ['葫芦丝'], '侗族': ['侗族大歌'], '哈萨克族': ['冬不拉', 'دومبىرا', 'قازاق ءانى'],
    '高山族': ['台湾少数民族音乐', '风潮唱片', '原住民歌手', '阿美族音乐', '排湾族音乐', '布农族音乐', '泰雅族音乐', '邹族音乐', '鲁凯族音乐', '卑南族音乐', '达悟族音乐', '原住民古谣'], '彝族': ['彝语歌曲'], '壮族': ['刘三姐', '广西山歌', '壮族山歌', '壮族天琴', '壮语歌曲'], '瑶族': ['瑶族舞曲'],
    '白族': ['大理白族调'], '哈尼族': ['哈尼古歌'], '纳西族': ['纳西古乐'], '土家族': ['龙船调'],
    '柯尔克孜族': ['玛纳斯'], '羌族': ['羌笛'], '赫哲族': ['乌苏里船歌'], '京族': ['过桥风吹'],
    '鄂伦春族': ['赞达仁'], '基诺族': ['基诺大鼓'], '塔吉克族': ['花儿为什么这样红'],
    '景颇族': ['目瑙纵歌', '石勒干', '鲍勒况', '董卫明', '鲍道龙', '孔黎明', '翁丽丽', '包木兰', '排木龙', '岳木果',
              '排当', '王玉兰', '沙宽娅', '景颇男儿', '森林乐队', '支丹山组合', 'Ah Ba Di', 'Aura Li', 'Galau Ting Luk',
              'Lahpai La Ja', 'Kumhtung Seng Ra', 'Jet San Htun', 'Nor Ni', 'Zatang Tu Hkawng', 'Y Ah Latt', 'Ja Moon Yi', 'Moses'],
    '回族': ['花儿'], '东乡族': ['东乡花儿'], '撒拉族': ['撒拉花儿'], '保安族': ['保安花儿'], '土族': ['土族花儿'],
    '布依族': ['好花红'], '傈僳族': ['摆时'], '佤族': ['月亮升起来'], '畲族': ['畲族山歌'],
    '水族': ['水族双歌'], '达斡尔族': ['乌钦'], '仫佬族': ['古条歌'], '布朗族': ['布朗弹唱'],
    '毛南族': ['毛南族欢'], '阿昌族': ['阿昌山歌'], '普米族': ['四弦琴'], '怒族': ['怒族达比亚'],
    '乌孜别克族': ['埃希来'], '俄罗斯族': ['俄罗斯族歌曲', 'русская песня'], '鄂温克族': ['敖鲁古雅'], '德昂族': ['德昂水鼓'],
    '裕固族': ['裕固族牧歌'], '塔塔尔族': ['塔塔尔族歌舞'], '独龙族': ['独龙江民歌'], '门巴族': ['门巴酒歌'],
    '珞巴族': ['珞巴古歌'], '黎族': ['黎族竹木器乐'], '仡佬族': ['仡佬族山歌'], '锡伯族': ['锡伯族民歌'],
}
# 55个民族的知名歌手/乐队/乐团知识库 (作为搜索种子, 逐位艺人搜索以穷尽其曲目)
ETHNIC_ARTISTS = {
    '蒙古族': ['腾格尔', '德德玛', '布仁巴雅尔', '乌兰图雅', '呼斯楞', '齐峰', '斯琴格日乐', '阿云嘎', '额尔古纳乐队', '杭盖乐队', '安达组合', '九宝乐队', '凤凰传奇', '乌兰托娅', '梅林组合', '傲日其楞'],
    '藏族': ['韩红', '降央卓玛', '容中尔甲', '亚东', '扎西顿珠', '蒲巴甲', '阿兰', '旦增尼玛', '谢旦', '高原红组合', '根呷', '琼雪卓玛'],
    '维吾尔族': ['克里木', '艾尔肯', '帕尔哈提', '阿布都拉'],
    '回族': ['苏尔东', '穆言', '马良玉', '韩生元', '撒丽娜', '安宝龙', '安宇歌', '塞里麦·安妮',
             '马关辉', '马月旭', '赵绍波', '马睿哲', '戚发旭', '哈辉', '马跃成', '赵云台',
             '马慧茹', '马太萱', '马汉东'],
    '苗族': ['宋祖英', '阿幼朵', '蝶当久'],
    '彝族': ['吉克隽逸', '莫西子诗', '海来阿木', '阿鲁阿卓', '山鹰组合', '彝人制造', '太阳部落'],
    '壮族': ['韦唯', '罗宁娜'],
    '朝鲜族': ['崔健', '阿里郎组合', '卞英花', '金润吉'],
    '满族': ['那英'],
    '侗族': ['吴虹飞'],
    '高山族': ['张惠妹', '动力火车', '高慧君', '王宏恩', '风潮唱片', '胡德夫', '陈建年', '纪晓君', '昊恩家家', '南王姐妹花', '北原山猫', '王宏恩', 'AMIS 旮亻乐团', '桑布伊', '以莉·高露', '阿爆', '戴晓君', '安溥', '巴奈', '糯米团', '图腾乐队', '拷秋勤', 'MATZKA', 'BOXING', '出口', '漂流出口', 'YOBO', '德路一族', '岚馨乐团', '马兰吟唱队', '郭英男', 'Difang', '北投普唭嗄岸', '泰武古谣传唱', '八部合音', '布农族八部合音', '台东马兰阿美', '兰屿达悟', '邹族', '赛德克', '泰雅', '排湾', '鲁凯', '卑南', '阿美族', '噶玛兰', '撒奇莱雅', '太鲁阁', '赛夏', '拉阿鲁哇', '卡那卡那富'],
    '_风潮唱片关键词': ['风潮唱片 台湾原住民', '风潮 胡德夫', '风潮 陈建年', '风潮 纪晓君', '风潮 桑布伊', '风潮 郭英男', '风潮 阿美', '风潮 卑南', '风潮 布农', '风潮 排湾', '风潮 泰雅', '风潮 邹族', '风潮 达悟', '风潮 台湾民歌', '风潮三十', '胡德夫 太平洋的风', '郭英男 老人饮酒歌', '阿美族民歌', '布农族 祈祷小米丰收歌', '泰雅族 口簧琴', '排湾族 古谣', '邹族 迎神曲', '达悟 拼板舟歌', '卑南族 南王', '赛德克 猎人歌', '鲁凯族 百合花', '噶玛兰 摇婴仔歌', '原住民古谣', '台湾原住民音乐'],
    '哈萨克族': ['塔斯肯', '叶尔波利'],
    '傣族': ['玉罕娇'],
    '白族': ['小阿鹏', '大理白族三道茶'],
    '土家族': ['土家族撒叶儿嗬', '土家摆手歌'],
    '哈尼族': ['哈尼多声部', '李弦', '米线'],
    '黎族': ['黎族姑娘', '五指山歌'],
    '瑶族': ['盘王大歌'],
    '佤族': ['佤族木鼓歌', '茶艾南'],
    '俄罗斯族': ['中国俄罗斯族合唱'],
    '锡伯族': ['锡伯族西迁之歌'],
}
ETHNIC_COLORS = [['#c21500', '#ffc500'], ['#8e2de2', '#4a00e0'], ['#136a8a', '#267871'], ['#cb2d3e', '#ef473a'],
                 ['#f7971e', '#ffd200'], ['#654ea3', '#eaafc8'], ['#02aab0', '#00cdac'], ['#42275a', '#734b6d'],
                 ['#20002c', '#cbb4d4'], ['#3a1c71', '#d76d77']]
# 回族精确化词表(deepseek 回族音乐人清单)。通用人名用合成词(名+回族/花儿)避免把流行歌灌进来。
# 必须在 ETHNIC_GROUPS 构建循环前完成, 否则不生效(藏族/佤族/傈僳族 augmentation 已被循环甩在身后, 是历史遗留死代码)。
ETHNIC_EXTRA_KW['回族'] = ETHNIC_EXTRA_KW.get('回族', []) + [
    '王秀芳 花儿', '王秀芳 回族', '张明星 花儿', '张明星 回族', '张建军 山花儿', '张建军 回族',
    '杨慈 回族', '杨慈 红河', '甄臻 回族', '李月波 昆明小调', '王一川 回族', '张莉 回族',
    '马晓燕 花儿', '马晓燕 回族', '陈红 回回人', '陈红 回族', '尔萨 回族', '丁旭阳 回族',
    '新疆花儿', '宁夏山花儿', '河湟花儿', '回族花儿', '回族宴席曲', '回族口弦', '门源宴席曲',
    '青海花儿 回族', '宁夏花儿 回族', '回族原生态民歌',
    '上去高山望平川', '眼泪花儿把心淹了', '园子里长着绿韭菜', '阿妈的盖碗茶', '吆骡子',
    '黑猫窝在锅台上', '尕老汉', '花花尕妹', '我和尕妹要团圆', '多斯塔尼安色俩目',
    '牵驼的阿哥赶路程', '请到回族山乡来', '心里的花儿漫上来', '抓发菜的尕姑娘',
    '这是我回族的金银川', '团结花开红艳艳', '青海花儿俊',
    '回族人', '回族姑娘', '红盖头的尕妹', '法特茉尔', '娘姥子心', '回族丫头你在哪达里',
    '郑和之歌', '马本斋之歌', '法图麦', '穆斯林姑娘', '我的幸福在哪哒尼', '端庄之歌',
    '阿哥的心疼尕妹', '娃是阿妈的心头肉', '回族丫头', '回回的味道', '幸福的花儿在绽放',
    '美丽的回回姑娘', '尕妹是我的白牡丹', '我在沙甸等你', '童年的缅桂花树下',
    '回族尕妹我的花儿', '白盖头黑眼睛', '尔代节的月亮', '宁夏川好地方', '阿哥的白牡丹',
    '回回人', '丝路回声', '拔了麦子拔胡麻',
]
ETHNIC_GROUPS = []
for _i, _name in enumerate(ETHNIC_BASE):
    _kws = [f'{_name}民歌', f'{_name}歌曲'] + ETHNIC_EXTRA_KW.get(_name, []) + ETHNIC_ARTISTS.get(_name, [])
    ETHNIC_GROUPS.append({'key': f'e{_i:02d}', 'name': _name, 'kws': _kws, 'ci': _i % 10})
# 汉族民间小调: 各省市民间小调搜索词(与民族音乐严格区分)
HAN_FOLK_KWS = [
    '汉族民间小调', '汉族民歌', '中国民歌', '民间小调', '民歌小调',
    '江苏民歌 茉莉花', '河北民歌 小白菜', '陕北民歌 信天游', '陕北民歌 山丹丹开花红艳艳', '甘肃民歌 花儿 汉族', '青海花儿 汉族',
    '山西民歌 走西口', '内蒙古汉族民歌 二人台', '东北民歌 小看戏', '东北民歌 摇篮曲', '山东民歌 包楞调', '山东民歌 沂蒙山小调',
    '河南民歌 编花篮', '湖北民歌 龙船调', '湖南民歌 洗菜心', '四川民歌 康定情歌', '四川民歌 太阳出来喜洋洋', '云南汉族民歌 小河淌水',
    '贵州汉族民歌', '广西汉族民歌', '广东客家山歌', '福建民歌 采茶灯', '浙江民歌 茉莉花', '安徽民歌 凤阳花鼓', '江西民歌 十送红军',
    '江苏民歌 拔根芦柴花', '上海民歌 紫竹调', '天津民歌 画扇面', '北京民歌', '海南民歌 请到天涯海角来', '台湾民歌 望春风',
    '河北民歌 回娘家', '山西民歌 桃花红杏花白', '陕西民歌 兰花花', '宁夏民歌 汉族', '新疆汉族民歌', '西藏汉族民歌',
    '江南丝竹 行街', '广东音乐 步步高', '潮州音乐 寒鸦戏水', '客家汉乐', '丝竹乐', '民乐合奏 金蛇狂舞', '民乐合奏 喜洋洋',
    '古琴曲 流水', '二胡曲 二泉映月', '琵琶曲 十面埋伏', '笛子曲 姑苏行', '古筝曲 渔舟唱晚', '唢呐曲 百鸟朝凤',
    '瑞鸣音乐 中国音乐地图', '中国音乐地图 听见汉族', '戏曲选段 京剧', '戏曲选段 越剧', '戏曲选段 黄梅戏', '戏曲选段 豫剧', '戏曲选段 昆曲',
]
HAN_EXTRA_ARTISTS = ['宋祖英', '阎维文', '蒋大为', '彭丽媛', '雷佳', '蔡国庆', '张也', '那英', '韦唯', '崔健', '祖海', '董文华', '凤凰传奇', '李谷一', '胡松华', '吴雁泽', '郁钧剑', '吕继宏', '刘和刚', '王二妮', '阿宝', '石占明', '云飞', '王向荣', '赵大地', '高保利', '刘秉义', '杨洪基', '戴玉强', '王宏伟', '张也', '陈思思', '汤灿', '祖海', '雷佳']
# 藏区新艺人(天杵乐队/阿佳组合等), 佤族乐队, 傈僳族组合
ETHNIC_ARTISTS['藏族'] = ETHNIC_ARTISTS.get('藏族', []) + ['天杵乐队', '阿佳组合', 'ANU阿努', 'ANU', '谢旦', '旦增尼玛', '尖参', '更却依林', '德格叶', '琼雪卓玛', '根呷', '三木科', '华尔贡', '容中尔甲', '亚东', '扎西尼玛', '岗毅', '青海湖组合', '藏人组合', '九宝乐队', 'HAYA乐团', '颠覆M乐队', '唐古拉风组合']
ETHNIC_ARTISTS['佤族'] = ETHNIC_ARTISTS.get('佤族', []) + ['卡佤乐队', '濮曼乐队', '佤族唱跳组合', '岩三根', '娜日', '艾芒', '佤邦之音']
ETHNIC_ARTISTS['傈僳族'] = ETHNIC_ARTISTS.get('傈僳族', []) + ['亚哈巴组合', '傈僳族摆时组合', '阿石才', '此路恒', '傈僳之音', '木金乐组合', '腊娜瓦丽组合']
# 回族精确化词表已上移到 ETHNIC_GROUPS 构建循环之前(否则不生效)。

# 景颇族精确化: 权威歌手库 (来自用户提供的 景颇歌手及歌曲.docx + 景颇歌单584首.md)
JINGPO_DOCX_ARTISTS = ['石勒干', '鲍勒况', '董卫明', '鲍道龙', '孔黎明', '进同干', '翁丽丽', '包木兰', '唐木锐', '刘永江',
                       '雷都', '何勒都', '排木龙', '梅普拥汤', '藏包干', '董云', '孔秀芬', '赵兰芳', '孔会英', '岳木果',
                       '岳木载', '排当', '董建斌', '李勒都', '王玉兰', '排南汤', '沙宽娅', '景颇男儿', '森林乐队', '景颇杜莫', '支丹山组合',
                       'Yup Seng Ing', 'Ah Ba Di', 'Ah Gyung', 'Doi Wawm', 'Lahpai La Ja', 'Kumhtung Seng Ra', 'Bawk Win',
                       'Jet San Htun', 'Galau Ting Luk', 'Aura Li', 'Lasham Hka Li', 'Nor Ni', 'Zatang Tu Hkawng',
                       'Mangshang Ah Wang', 'Ah Moon', 'Ning Jar', 'Hpauda Goon', 'Y Ah Latt', 'GD Nbrang', 'Ja Moon Yi',
                       'Nde Ja Sam', 'Langa Tsin Tsin', 'Wadu Pri Lum', 'K. Ja Nu', 'Esabel Ja Bawk', 'Bola', 'Zung Ki',
                       'Nan Ni', 'Moses', 'Ah Ze', 'Ong Lar', 'Minzai Lum Naw', 'Chan Khin', 'Dau Lum', 'Nding Ah Ja',
                       'Lashi Hkawn Hkawn', 'D Ah Tu', 'Pawm Mung San', 'Brang San', 'MM5', 'Tangbau Gum Seng Naw',
                       'Gum San', 'Jan Pan', 'Bungshi Pan', 'Maran Seng Naw', 'P. Ja Seng Bu', 'Nding Gum Nan', 'K. Ah Brang', 'Doi San Lar']
JINGPO_MD_ARTISTS = ['Lahpai Jar Jar', 'HOWA GAM', 'Sut Seng Awng', 'Htoi San Pan', 'Sut Ring San', 'JaSinTa Ing',
                     'N D Sut Ing', 'Lahkri Htu Shan', 'KD Ah Htung', 'Lahtaw Ja Tsin', 'Kareng Kai Din', 'April Hkawn Din',
                     'Galau Zung Ki', 'Mai Nu', 'Tsawm Tsawm Pan', 'Ann Nau', 'Ah Doi', 'Fostina Lagang', 'Maran Seng Naw',
                     'ZahkungJeNyoi', 'Karu zeng', 'Lahtaw Zau Lum', 'Zau Doi Hkawng', 'Maraw Brang Mai', 'Mai Mai Seng',
                     'Lawi Chang Ting', 'Nchyang Hka Ra', 'Hka hkrang Zau Sam', 'Du Nhkum Jaw Dan Awng', 'Ding Zai Awng', 'JZ Dau Lum']
JINGPO_GENRE_KWS = ['景颇民歌', '景颇歌曲', '景颇族歌曲', '目瑙纵歌', '目瑙纵歌节', '景颇音乐', '吐良', '景颇语歌曲',
                    'jinghpaw mahkawn', 'kachin song', 'kachin traditional song', 'manau song', 'zaiwa mahkawn', 'jinghpaw']
JINGPO_MAX_SONGS = 800
jingpo_state = {'state': 'idle', 'progress': '', 'count': 0}


def build_jingpo_deep():
    """景颇族精确化深挖: 81位权威歌手+31位md艺人+体裁词遍历六源(酷狗酷我40深), YouTube补充; 突破200首上限"""
    group_key, info = 'e26', ETHNIC_BY_KEY['e26']
    with ethnos_lock:
        if jingpo_state['state'] == 'building':
            return False
        jingpo_state.update(state='building', progress='准备中', count=0)
        ethnos_state['e26'] = {'state': 'building', 'count': ethnos_state.get('e26', {}).get('count', 0)}
    try:
        client = _ethnos_get_client()
        merged = {}
        # 热启动: 已有景颇歌单先进池
        old_path = _cache_path('e26')
        with suppress(Exception):
            if old_path.exists():
                _old = json.loads(old_path.read_text(encoding='utf-8'))
                if _old.get('v') == ETHNOS_SCHEMA:
                    from musicdl.modules import SongInfo as _SI
                    for _t in _old.get('tracks') or []:
                        with suppress(Exception):
                            _s = _SI.fromdict(_t.get('_song') or {})
                            if _s.song_name:
                                merged[_dedup_key(_s)] = (_s, 1000)
        all_artists = JINGPO_DOCX_ARTISTS + [a for a in JINGPO_MD_ARTISTS if a not in JINGPO_DOCX_ARTISTS]

        def merge_in(songs, weight):
            for s in songs:
                if not s.song_name:
                    continue
                key = _dedup_key(s)
                if not key.strip('|'):
                    continue
                singer_hit = any(a.lower() in str(s.singers or '').lower() for a in all_artists)
                score = (weight + (2000 if singer_hit else 0)
                         + (100 if s.protocol == 'HTTP' and isinstance(s.download_url, str) and s.download_url.startswith('http') else 0)
                         + (10 if str(s.ext or '').upper() in {'FLAC', 'WAV', 'APE'} else 0))
                prev = merged.get(key)
                if prev is None or score > prev[1]:
                    merged[key] = (s, score)

        def flush_partial():
            songs_sorted = [v[0] for v in sorted(merged.values(), key=lambda x: -x[1])][:JINGPO_MAX_SONGS]
            _atomic_write_json(_cache_path('e26'), _ethnos_payload(info, 'e26', songs_sorted, partial=True))
            with ethnos_lock:
                jingpo_state['count'] = len(songs_sorted)
        # 1) 六源逐位艺人 (酷狗/酷我40深)
        for i, artist in enumerate(all_artists):
            if ethnos_stop.is_set():
                break
            with ethnos_lock:
                jingpo_state['progress'] = f'歌手 {i+1}/{len(all_artists)}: {artist}'
            merge_in(_search_ethnos_keyword(client, artist), 5000)
            flush_partial()
            time.sleep(2)
        # 2) 六源体裁词
        for i, kw in enumerate(JINGPO_GENRE_KWS):
            if ethnos_stop.is_set():
                break
            with ethnos_lock:
                jingpo_state['progress'] = f'体裁词 {i+1}/{len(JINGPO_GENRE_KWS)}: {kw}'
            merge_in(_search_ethnos_keyword(client, kw), 4000)
            flush_partial()
            time.sleep(2)
        # 3) YouTube 补充: 体裁词 + 头部艺人
        try:
            yt_client = musicdl.MusicClient(music_sources=['YouTubeMusicClient'],
                                            init_music_clients_cfg={'YouTubeMusicClient': {'work_dir': str(DOWNLOAD_DIR), 'search_size_per_source': 10, 'max_retries': 1, 'disable_print': True}})
            yt_kws = ['jinghpaw mahkawn', 'kachin song', 'kachin traditional', 'manau song', 'zaiwa mahkawn'] + JINGPO_DOCX_ARTISTS[31:43]
            for i, kw in enumerate(yt_kws):
                if ethnos_stop.is_set():
                    break
                with ethnos_lock:
                    jingpo_state['progress'] = f'YouTube {i+1}/{len(yt_kws)}: {kw}'
                with suppress(Exception):
                    merge_in(yt_client.music_clients['YouTubeMusicClient'].search(keyword=kw, num_threadings=3, request_overrides={}, rule={}) or [], 3000)
                flush_partial()
                time.sleep(3)
        except Exception:
            pass
        songs = [v[0] for v in sorted(merged.values(), key=lambda x: -x[1])][:JINGPO_MAX_SONGS]
        if not songs:
            with ethnos_lock:
                jingpo_state.update(state='idle', progress='', count=0)
            return False
        _atomic_write_json(_cache_path('e26'), _ethnos_payload(info, 'e26', songs, partial=False))
        with ethnos_lock:
            ethnos_state['e26'] = {'state': 'done', 'count': len(songs)}
            jingpo_state.update(state='done', progress='', count=len(songs))
        return True
    except Exception:
        with ethnos_lock:
            jingpo_state.update(state='idle', progress='', count=0)
        return False


def build_jingpo_youtube_patch():
    """YouTube 定向补搜: 对六源0命中的景颇艺人逐位搜YouTube, 特征过滤后并入e26 (慢, 后台跑)"""
    import re as _re
    group_key, info = 'e26', ETHNIC_BY_KEY['e26']
    with ethnos_lock:
        if jingpo_state['state'] == 'building':
            return False
        jingpo_state.update(state='building', progress='YouTube补搜·准备中', count=jingpo_state.get('count', 0))
        ethnos_state['e26'] = {'state': 'building', 'count': ethnos_state.get('e26', {}).get('count', 0)}
    try:
        # 特征集(与净化一致)
        all_artists = JINGPO_DOCX_ARTISTS + JINGPO_MD_ARTISTS
        genre = ['景颇', 'jinghpaw', 'jingpo', 'kachin', 'manau', 'zaiwa', 'mahkawn', '目瑙', 'wunpawng', '吐良', 'singpho', '勒尺', '哦然', '德宏']
        artists_norm = {_re.sub(r'[\s\-—·,，.。\'"()（）【】\[\]]', '', a.lower()) for a in all_artists if len(a) >= 4}
        # 找出 e26 中 0 命中的艺人
        payload = json.loads((_cache_path('e26')).read_text(encoding='utf-8')) if (_cache_path('e26')).exists() else {'tracks': []}
        existing_singers = ' || '.join(str(t.get('singers') or '') for t in payload.get('tracks') or []).lower()
        # YouTube 上景颇内容用景颇语/拉丁名发布, 纯中文名艺人跳过(命中率趋零且耗时)
        def _is_latin(a):
            return any('\u4e00' <= ch <= '\u9fff' for ch in a) is False
        missing = [a for a in all_artists if a.lower() not in existing_singers and _is_latin(a)]
        with ethnos_lock:
            jingpo_state['progress'] = f'YouTube补搜·{len(missing)}位缺位拉丁名艺人'
        yt_client = musicdl.MusicClient(music_sources=['YouTubeMusicClient'],
                                        init_music_clients_cfg={'YouTubeMusicClient': {'work_dir': str(DOWNLOAD_DIR), 'search_size_per_source': 5, 'max_retries': 1, 'disable_print': True}})
        from musicdl.modules import SongInfo as _SI
        merged = {}
        for _t in payload.get('tracks') or []:
            with suppress(Exception):
                _s = _SI.fromdict(_t.get('_song') or {})
                if _s.song_name:
                    merged[_dedup_key(_s)] = (_s, 1000)
        added = 0
        for i, artist in enumerate(missing):
            if ethnos_stop.is_set():
                break
            with ethnos_lock:
                jingpo_state['progress'] = f'YouTube补搜 {i+1}/{len(missing)}: {artist}'
            songs = []
            with suppress(Exception):
                songs = yt_client.music_clients['YouTubeMusicClient'].search(keyword=artist, num_threadings=5, request_overrides={}, rule={}) or []
            for s in songs:
                if not s.song_name:
                    continue
                singer, title = str(s.singers or '').lower(), str(s.song_name or '').lower()
                first = _re.sub(r'[\s\-—·,，.。\'"()（）【】\[\]]', '', str(s.singers or '').split('/')[0].split(',')[0].lower())
                hit = (artist.lower() in singer or artist.lower() in title
                       or any(a in first for a in artists_norm if a)
                       or any(g in title for g in genre))
                if not hit:
                    continue   # 无关结果(如华语流行)不入库
                key = _dedup_key(s)
                prev = merged.get(key)
                score = 4000 + (100 if s.protocol == 'HTTP' and isinstance(s.download_url, str) and s.download_url.startswith('http') else 0)
                if prev is None:
                    added += 1
                    merged[key] = (s, score)
                elif score > prev[1]:
                    merged[key] = (s, score)
            # 定期落盘
            if (i + 1) % 3 == 0 or i == len(missing) - 1:
                songs_sorted = [v[0] for v in sorted(merged.values(), key=lambda x: -x[1])][:JINGPO_MAX_SONGS]
                _atomic_write_json(_cache_path('e26'), _ethnos_payload(info, 'e26', songs_sorted, partial=True))
                with ethnos_lock:
                    jingpo_state['count'] = len(songs_sorted)
        songs = [v[0] for v in sorted(merged.values(), key=lambda x: -x[1])][:JINGPO_MAX_SONGS]
        _atomic_write_json(_cache_path('e26'), _ethnos_payload(info, 'e26', songs, partial=False))
        with ethnos_lock:
            ethnos_state['e26'] = {'state': 'done', 'count': len(songs)}
            jingpo_state.update(state='done', progress=f'YouTube补搜完成, 新增{added}首', count=len(songs))
        return True
    except Exception:
        with ethnos_lock:
            jingpo_state.update(state='done', progress='', count=jingpo_state.get('count', 0))
            ethnos_state['e26'] = {'state': 'done', 'count': ethnos_state.get('e26', {}).get('count', 0)}
        return False


def build_bilibili_ethnos(group_key):
    """B站深挖: 用该民族全部关键词单独遍历B站(音频区+视频音轨), 标题/UP主含民族特征才收, 合并写盘"""
    info = ETHNIC_BY_KEY[group_key]
    with ethnos_lock:
        if ethnos_state[group_key]['state'] == 'building':
            return False
        prev = ethnos_state[group_key].get('count', 0)
        ethnos_state[group_key] = {'state': 'building', 'count': prev}
    try:
        # 每轮用全新 Bilibili 客户端: 全局单例被连续 B站请求打坏后会稳定返回0,
        # 导致 bili_all 空转(实测全新客户端能正常搜到增量)。见 2026-09-28 诊断。
        from musicdl.modules.sources import bilibili as _bili_mod
        bili = _bili_mod.BilibiliMusicClient()
        merged = {}
        warm_keys = set()
        old_path = _cache_path(group_key)
        with suppress(Exception):
            if old_path.exists():
                _old = json.loads(old_path.read_text(encoding='utf-8'))
                if _old.get('v') == ETHNOS_SCHEMA:
                    from musicdl.modules import SongInfo as _SI
                    for _t in _old.get('tracks') or []:
                        with suppress(Exception):
                            _s = _SI.fromdict(_t.get('_song') or {})
                            if _s.song_name:
                                _k = _dedup_key(_s)
                                _prev = merged.get(_k)
                                _yt = isinstance(_s.download_url, str) and _s.download_url.startswith('ytdlp:')
                                _prev_yt = bool(_prev) and isinstance(_prev[0].download_url, str) and str(_prev[0].download_url).startswith('ytdlp:')
                                # 同名冲突时国内源优先(直连快速), YouTube仅作国内没有时的补充
                                if _prev is None or ((not _yt) and _prev_yt):
                                    merged[_k] = (_s, 1000)
                                    warm_keys.add(_k)
        kws = info['kws']
        feats = [str(k).lower() for k in kws] + [info['name'].removesuffix('族'), info['name']]
        added = 0
        for kw_idx, kw in enumerate(kws):
            if ethnos_stop.is_set():
                break
            try:
                songs = bili.search(keyword=kw, num_threadings=4, request_overrides={}, rule={}) or []
            except Exception:
                songs = []
            for s in songs:
                if not s.song_name:
                    continue
                blob = f"{s.song_name} {s.singers or ''}".lower()
                if not any(f in blob for f in feats if len(f) >= 2):
                    continue   # B站搜索模糊, 标题/UP主均无民族特征则剔除
                key = _dedup_key(s)
                if not key.strip('|') or key in warm_keys:
                    continue   # 增量语义: 老歌永不被新结果覆盖
                score = (800 + (100 if s.protocol == 'HTTP' and isinstance(s.download_url, str) and s.download_url.startswith('http') else 0)
                         + (10 if str(s.ext or '').upper() in {'FLAC', 'WAV', 'APE'} else 0))   # 低于热启动老歌(1000): B站为补充而非替换
                prev2 = merged.get(key)
                if prev2 is None:
                    added += 1
                    merged[key] = (s, score)
                elif score > prev2[1]:
                    merged[key] = (s, score)
            with ethnos_lock:
                ethnos_state[group_key]['count'] = min(len(merged), max(ETHNOS_MAX_SONGS, prev))
            # 每词落盘防中断丢失
            songs_sorted = [v[0] for v in _incremental_merge(merged, warm_keys)]
            _atomic_write_json(_cache_path(group_key), _ethnos_payload(info, group_key, songs_sorted, partial=True))
            time.sleep(2)
        songs = [v[0] for v in _incremental_merge(merged, warm_keys)]
        if not songs:
            with ethnos_lock:
                ethnos_state[group_key] = {'state': 'done', 'count': prev}
            return False
        _atomic_write_json(_cache_path(group_key), _ethnos_payload(info, group_key, songs, partial=False))
        with ethnos_lock:
            ethnos_state[group_key] = {'state': 'done', 'count': len(songs)}
        return True
    except Exception:
        with ethnos_lock:
            ethnos_state[group_key] = {'state': 'done', 'count': prev}
        return False


def bilibili_build_all_worker():
    """B站深挖全量: 景颇优先, 两路并行, 每路内顺序节流
    注意: 这里原先误调 build_ethnos_playlist(国内七源全量重建), 点「B站深挖全量」
    实际完全没碰 B站。已修正为 build_bilibili_ethnos(与 L2239 单民族入口一致)。
    """
    order = ['e26'] + [g['key'] for g in ETHNIC_GROUPS if g['key'] != 'e26']
    pending = list(order)

    def consume():
        while True:
            if ethnos_stop.is_set():
                return
            try:
                key = pending.pop(0)
            except IndexError:
                return
            ok = build_bilibili_ethnos(key)
            time.sleep(15 if not ok else 6)
    workers = [threading.Thread(target=consume, daemon=True) for _ in range(2)]
    [w.start() for w in workers]
    [w.join() for w in workers]


ETHNIC_CONFIRMED = {artist: ethnic for ethnic, lst in ETHNIC_ARTISTS.items() for artist in lst}
ETHNIC_BY_KEY = {g['key']: g for g in ETHNIC_GROUPS}


_CACHE_KEY_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def _cache_path(group_key):
    """民族/汉族歌单文件路径: 统一入口, 优先人类可读新名(eNN_族名.json), 回退旧名(eNN.json)
    注意: 这里曾经写成递归调用自身(旧实现), 未知 key 或缓存缺失时会 RecursionError 直接打 500。
    现已改为显式旧名回退, 并对 group_key 做白名单校验, 顺带堵住路径穿越。
    """
    # 白名单: 只接受 e01/e26/han 这类标识, 其余(含 ../ 之类)一律落到安全兜底名
    if not isinstance(group_key, str) or not _CACHE_KEY_RE.match(group_key):
        group_key = _norm_path(group_key)
    if group_key == 'han':
        return ETHNOS_CACHE_DIR / 'han_汉族民间小调.json'
    # 专题库(tNN_名称.json): 不在 56 民族之列的独立曲库(如摩梭), 同目录存放但与民族互不干扰
    if isinstance(group_key, str) and re.fullmatch(r't\d+', group_key):
        _hits = sorted(ETHNOS_CACHE_DIR.glob(f'{group_key}_*.json'))
        return _hits[0] if _hits else ETHNOS_CACHE_DIR / f'{group_key}.json'
    _old = ETHNOS_CACHE_DIR / f"{group_key}.json"
    _g = ETHNIC_BY_KEY.get(group_key)
    if _g:
        _new = ETHNOS_CACHE_DIR / f"{group_key}_{_g['name']}.json"
        # 新名已存在, 或旧名也不存在(首次构建) -> 用新名; 否则沿用旧名保持向后兼容
        if _new.exists() or not _old.exists():
            return _new
    return _old


ETHNOS_SCHEMA = 2
# 民族音乐构建音源: 实测能稳定返回中文歌曲的主力音源
ETHNOS_SOURCES = ['MyFreeMP3MusicClient', 'MiguMusicClient', 'TwoT58MusicClient', 'KuwoMusicClient', 'KugouMusicClient', 'YinyuekuMusicClient', 'BilibiliMusicClient']
ETHNOS_CACHE_DIR = BASE_DIR / 'webui' / 'ethnos_cache'
ETHNOS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
SINGER_CACHE_DIR = ETHNOS_CACHE_DIR / 'singers'
SINGER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
SINGER_MAX_SONGS = 300
# UI 态持久化: 卡片/歌手拖动次序、我的歌单、隐藏曲目、民族歌手增删名单等。
# 刻意放在 webui/ 而非 ethnos_cache/ —— 后者会被 e*.json / han_*.json 的
# 全量扫描(_iter_ethnos_tracks/_ethnos_files_sig)当作曲库文件吃掉。
UI_STATE_PATH = BASE_DIR / 'webui' / 'ui_state.json'
UI_STATE_LOCK = threading.Lock()


def _track_rawkey(t):
    """曲目原始复合键(不规范化): 置顶/次序存储用, 与前端 ethTrackKey 完全一致"""
    return ('\x1f'.join([str(t.get('song_name') or ''), str(t.get('singers') or ''), str(t.get('source') or '')]))


def _track_key(t):
    """曲目复合键: 歌名+歌手+音源, 用于精确定位单条曲目。
    只按歌名匹配会把同名不同歌手的版本一并删除(景颇族《目瑙纵歌》实测一次误删 24 条)。"""
    return (_norm_str(t.get('song_name')), _norm_str(t.get('singers')), _norm_str(t.get('source')))
singer_building, singer_lock = set(), threading.Lock()


def _norm_path(s):
    import re as _re
    return _re.sub(r'[^\w\u4e00-\u9fff]+', '_', str(s)).strip('_')[:80] or 'unnamed'


def build_singer_playlist(name):
    """全网搜索某歌手/乐队的全部歌曲, 去重后缓存到磁盘 (穷尽式收录)"""
    with singer_lock:
        if name in singer_building:
            return False
        singer_building.add(name)
    try:
        client = _ethnos_get_client()
        merged = {}
        for song in _search_ethnos_keyword(client, name):
            if not song.song_name:
                continue
            key = _dedup_key(song)
            if not key.strip('|'):
                continue
            score = ((100 if song.protocol == 'HTTP' and isinstance(song.download_url, str) and song.download_url.startswith('http') else 0)
                     + (10 if str(song.ext or '').upper() in {'FLAC', 'WAV', 'APE'} else 0)
                     + (50 if str(name).lower() in str(song.singers or '').lower() else 0))
            prev = merged.get(key)
            if prev is None or score > prev[1]:
                merged[key] = (song, score)
        songs = [v[0] for v in sorted(merged.values(), key=lambda x: -x[1])][:SINGER_MAX_SONGS]
        payload = {'v': 1, 'name': name, 'at': time.time(), 'count': len(songs),
                   'tracks': [dict(song_brief(s, 'Singer', i), _song=s.todict()) for i, s in enumerate(songs)]}
        _atomic_write_json(SINGER_CACHE_DIR / f'{_norm_path(name)}.json', payload)
        return True
    finally:
        with singer_lock:
            singer_building.discard(name)
ETHNOS_MAX_SONGS = 200
ETHNOS_NEW_QUOTA = 200   # 增量语义: 已有曲目永不减少, 每轮深挖最多新增200首

ethnos_state = {g['key']: {'state': 'idle', 'count': 0} for g in ETHNIC_GROUPS}
ethnos_lock = threading.Lock()
ethnos_client = None
ethnos_stop = threading.Event()
# 用户最近一次搜索/试听/解析时刻: 民族歌单后台构建在用户活跃时自动让路, 避免抢占音源接口
last_user_activity = [0.0]


def touch_user_activity():
    last_user_activity[0] = time.time()


def user_is_active(seconds = 25):
    return time.time() - last_user_activity[0] < seconds


def _norm_str(s):
    import re as _re
    return _re.sub(r'[\s\-—·,，.。\'"()（）【】\[\]~～!！?？:：;；&+]', '', str(s or '').lower())


def _core_name(name):
    """字符串版归一化歌名: 去括号注记/版本后缀(Live/伴奏/翻唱/DJ等), A-B 交换视为同名。
    与 _core_songkey 同逻辑, 但入参是字符串(供歌曲检索建立匹配键), 不改动 _core_songkey 的对外行为。"""
    import re as _re
    raw = str(name or '')
    n = _re.sub(r'[（(\[【][^)）\]】]*[)）\]】]', '', raw)
    n = _re.sub(r'(现场版?|live版?|伴奏版?|纯音乐版?|翻唱版?|remix|dj版?|cover版?|卡拉ok版?)$', '', n.strip().lower())
    n = _norm_str(n)
    parts = sorted(p for p in (_norm_str(x) for x in _re.split(r'[-—]', raw)) if p)
    if len(parts) >= 2 and n == ''.join(parts):
        n = ''.join(sorted(parts))
    return n or _norm_str(raw)


def _core_songkey(song):
    """归一化歌名作去重键(薄封装, 逻辑同 _core_name)"""
    return _core_name(song.song_name)


def _dedup_key(song):
    first_singer = _norm_str(str(song.singers or '').split('/')[0].split(',')[0].split('、')[0])
    return _core_songkey(song) + '|' + first_singer


def _ethnos_get_client():
    global ethnos_client
    if ethnos_client is None:
        cfg = {}
        for s in ETHNOS_SOURCES:
            c = {'work_dir': str(DOWNLOAD_DIR), 'search_size_per_source': 25, 'max_retries': 1}
            if s in {'KugouMusicClient', 'KuwoMusicClient'}:
                c['search_size_per_source'] = 40   # 酷狗/酷我 UGC 曲库深: 用户上传的民族原生态内容多, 加大翻页深度
            cfg[s] = c
        ethnos_client = musicdl.MusicClient(music_sources=list(ETHNOS_SOURCES), init_music_clients_cfg=cfg)
    return ethnos_client


def _search_ethnos_keyword(client, keyword):
    """单关键词: 各音源并行搜索, 返回 SongInfo 列表; 用户活跃时提前让路"""
    results, lock = {}, threading.Lock()

    def worker(src):
        try:
            songs = client.music_clients[src].search(keyword=keyword, num_threadings=2, request_overrides={}, rule={}) or []
        except Exception:
            songs = []
        with lock:
            results[src] = songs
    threads = [threading.Thread(target=worker, args=(s,), daemon=True) for s in ETHNOS_SOURCES]
    [t.start() for t in threads]
    start_at, deadline = time.time(), time.time() + 150
    while True:
        if not any(t.is_alive() for t in threads):
            break
        if time.time() > deadline:
            break
        time.sleep(2)
    with lock:
        snapshot = {k: list(v) for k, v in results.items()}
    return [s for src in ETHNOS_SOURCES for s in snapshot.get(src, [])]


VERSION_KEEP = 1  # versions/ 每个主文件保留的历史版数(只留上一版做后悔药, 磁盘占用最小)


def _atomic_write_json(path, payload):
    """原子写入: 先写临时文件再改名; 覆盖前把旧版本归档到 versions/ (保留最近 VERSION_KEEP 版, 任何改动可回滚)"""
    import shutil as _shutil
    if path.exists():
        with suppress(Exception):
            vdir = path.parent / 'versions'
            vdir.mkdir(exist_ok=True)
            _shutil.copy2(path, vdir / f'{path.stem}.{int(time.time())}{path.suffix}')
            versions = sorted(vdir.glob(f'{path.stem}.*{path.suffix}'), key=lambda x: x.stat().st_mtime)
            for old_v in versions[:-VERSION_KEEP]:
                with suppress(Exception):
                    old_v.unlink()
    # 全字段净化: 剔除孤立代理字符/控制符(外部来源的正文常携带, 会污染UTF-8)
    def _clean(o):
        if isinstance(o, dict): return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, list): return [_clean(x) for x in o]
        if isinstance(o, str): return ''.join(c for c in o if not (0xD800 <= ord(c) <= 0xDFFF) and (ord(c) >= 32 or c in '\n\t'))
        return o
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(_clean(payload), ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    tmp.replace(path)


_SONG_SLIM_DROP = ('download_url_status', 'downloaded_contents', '_save_path', 'work_dir',
                   'chunk_size', 'default_download_headers', 'default_download_cookies')
_RAW_DATA_KEEP_SOURCES = {'BilibiliMusicClient', 'WeixinMusicClient', 'Bilibili', 'Weixin'}   # 自愈需从 raw_data 找 BV号/文章voice_id(含裸标签)

def _slim_song(sd):
    """落盘前裁剪 _song 冗余字段(2026-10-05 瘦身): download_url_status/原始下载参数等运行时不再读取,
    raw_data 仅保留 B站/微信(自愈链依赖), 其余源删除 —— 库文件体积 -25% 左右, 行为不变。"""
    if not isinstance(sd, dict):
        return sd
    if sd.get('source') not in _RAW_DATA_KEEP_SOURCES:
        sd.pop('raw_data', None)
    for k in _SONG_SLIM_DROP:
        sd.pop(k, None)
    return sd

def _ethnos_payload(info, group_key, songs, partial=False):
    tracks_payload = []
    for i, s in enumerate(songs):
        brief = song_brief(s, 'Ethnos', i)
        real = (s.source or '').removesuffix('MusicClient')
        if isinstance(s.download_url, str) and s.download_url.startswith('ytdlp:'):
            real = 'Bilibili' if 'bilibili.com' in s.download_url else 'YouTube'   # yt-dlp通道条目按目标域名还原音源标签
        elif isinstance(s.download_url, str) and 'res.wx.qq.com' in s.download_url:
            real = '光音·微信'   # 微信公众号音频同样借用下载通道, 按域名还原标签
        brief['source'] = real or 'Ethnos'
        brief['source_cn'] = SOURCE_NAMES.get(real, real) if real else '民族音乐'
        if not brief['duration'] or ':' not in brief['duration']:
            brief['duration'] = ''
        tracks_payload.append(dict(brief, _song=_slim_song(s.todict())))
    return {
        'v': ETHNOS_SCHEMA, 'partial': partial, 'name': f"{info['name']} · 民族音乐",
        'group': info['name'], 'key': group_key,
        'cover': (songs[0].cover_url if songs and songs[0].cover_url else ''),
        'count': len(songs), 'built_at': time.time(), 'kws': info['kws'],
        'sources': ETHNOS_SOURCES, 'tracks': tracks_payload,
    }




def _incremental_merge(merged, warm_keys):
    """增量语义: 热启动老歌(warm_keys)与新搜索结果全量保留, 无任何上限, 仅按分数排序展示"""
    return sorted(merged.values(), key=lambda x: -x[1])


def build_ethnos_playlist(group_key):
    """构建单个民族的歌单: 民族词+特色曲种+知名艺人逐位遍历全部音源, 严格去重, 每个关键词后立即落盘防丢失"""
    info = ETHNIC_BY_KEY[group_key]
    with ethnos_lock:
        if ethnos_state[group_key]['state'] == 'building':
            return False
        ethnos_state[group_key]['state'] = 'building'
        ethnos_state[group_key]['count'] = 0
    try:
        client = _ethnos_get_client()
        merged = {}
        warm_keys = set()
        # 热启动: 已有缓存的歌曲先进池(受保护, 永不被挤出)
        old_path = _cache_path(group_key)
        with suppress(Exception):
            if old_path.exists():
                _old = json.loads(old_path.read_text(encoding='utf-8'))
                if _old.get('v') == ETHNOS_SCHEMA:
                    from musicdl.modules import SongInfo as _SI
                    for _t in _old.get('tracks') or []:
                        with suppress(Exception):
                            _s = _SI.fromdict(_t.get('_song') or {})
                            if _s.song_name:
                                _k = _dedup_key(_s)
                                _prev = merged.get(_k)
                                _yt = isinstance(_s.download_url, str) and _s.download_url.startswith('ytdlp:')
                                _prev_yt = bool(_prev) and isinstance(_prev[0].download_url, str) and str(_prev[0].download_url).startswith('ytdlp:')
                                # 同名冲突时国内源优先(直连快速), YouTube仅作国内没有时的补充
                                if _prev is None or ((not _yt) and _prev_yt):
                                    merged[_k] = (_s, 1000)
                                    warm_keys.add(_k)
        artist_seeds = set(ETHNIC_ARTISTS.get(info['name'], []))
        for kw_idx, kw in enumerate(info['kws']):
            if ethnos_stop.is_set():
                break
            # 全面放开: 跑完全部关键词, 收益全收
            is_artist = kw in artist_seeds
            for song in _search_ethnos_keyword(client, kw):
                if not song.song_name:
                    continue
                key = _dedup_key(song)
                if not key.strip('|'):
                    continue
                if key in warm_keys:
                    continue   # 增量语义: 老歌永不被新结果覆盖
                prev = merged.get(key)
                score = ((3 if is_artist else (2 if any(w in str(song.song_name) for w in info['kws']) else 1)) * 1000
                         + (100 if song.protocol == 'HTTP' and isinstance(song.download_url, str) and song.download_url.startswith('http') else 0)
                         + (10 if str(song.ext or '').upper() in {'FLAC', 'WAV', 'APE'} else 0))
                if prev is None or score > prev[1]:
                    merged[key] = (song, score)
            with ethnos_lock:
                ethnos_state[group_key]['count'] = min(len(merged), ETHNOS_MAX_SONGS)
            # 每个关键词完成即落盘, 中断不丢已搜结果
            songs_sorted = [v[0] for v in _incremental_merge(merged, warm_keys)]
            _atomic_write_json(_cache_path(group_key), _ethnos_payload(info, group_key, songs_sorted, partial=True))
        songs = [v[0] for v in _incremental_merge(merged, warm_keys)]
        if not songs:
            # 空结果(通常为限流): 不写盘, 状态回退待重试
            with ethnos_lock:
                ethnos_state[group_key] = {'state': 'idle', 'count': 0}
            return False
        _atomic_write_json(_cache_path(group_key), _ethnos_payload(info, group_key, songs, partial=False))
        with ethnos_lock:
            ethnos_state[group_key] = {'state': 'done', 'count': len(songs)}
        return True
    except Exception:
        with ethnos_lock:
            ethnos_state[group_key] = {'state': 'idle', 'count': 0}
        return False


def load_ethnos_playlist(group_key):
    """读取磁盘缓存的民族歌单, 恢复 SongInfo 并注册进可播放/可下载的会话"""
    from musicdl.modules import SongInfo as _SongInfo
    path = _cache_path(group_key)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload.get('v') != ETHNOS_SCHEMA:
        return None
    client = _ethnos_get_client()
    sid = new_session(client)
    items = search_sessions[sid]['items']
    # 置顶/拖动次序(服务端单一事实来源): 置顶在前(按其列表序), 其余按 track_order, 无记录的保持原序
    _tp = payload.get('tracks_pinned') or []
    _to = payload.get('track_order') or []
    raw_tracks = payload.get('tracks') or []
    _tkey = _track_rawkey
    _pin_idx = {k: i for i, k in enumerate(_tp)}
    _ord_idx = {k: i for i, k in enumerate(_to)}
    # 曲名归一化簇键: 裸名认领(「雷佳 - 茉莉花」并进《茉莉花》簇)
    import re as _re4
    def _cpart(x):
        _n = _re4.sub(r'[（(\[【][^)）\]】]*[)）\]】]', '', str(x or ''))
        _n = _re4.sub(r'(现场版?|live版?|伴奏版?|纯音乐版?|翻唱版?|remix|dj版?|cover版?|卡拉ok版?)$', '', _n.strip().lower())
        return _norm_str(_n)
    _bare = {_cpart(str(_t.get('song_name') or '')) for _t in raw_tracks if not _re4.search(r'[-—]', str(_t.get('song_name') or ''))}
    _bare.discard('')
    def _cluster_key(nm):
        _raw = str(nm or '')
        if not _re4.search(r'[-—]', _raw):
            return _cpart(_raw) or _norm_str(_raw)
        _parts = [p for p in (_cpart(x) for x in _re4.split(r'[-—]', _raw)) if p]
        if not _parts:
            return _norm_str(_raw)
        _cands = [p for p in _parts if p in _bare]
        if _cands:
            return max(_cands, key=len)
        return ''.join(sorted(_parts))
    _INSTR = _re4.compile(r'伴奏|纯音乐|instrumental|karaoke|卡拉ok|配乐版', _re4.I)
    first_seen, cluster_members = {}, {}
    for _i, _t in enumerate(raw_tracks):
        _c = _cluster_key(_t.get('song_name'))
        first_seen.setdefault(_c, _i)
        cluster_members.setdefault(_c, []).append(_i)
    def _intra(_t):
        try:
            _br = int(_t.get('bitrate') or 0)
        except Exception:
            _br = 0
        try:
            _sz = int((_t.get('_song') or {}).get('file_size_bytes') or 0)
        except Exception:
            _sz = 0
        _lossless = str(_t.get('ext') or '').upper() in ('FLAC', 'WAV', 'APE', 'ALAC')
        return (1 if _INSTR.search(str(_t.get('song_name') or '')) else 0,
                0 if _lossless else 1, -_br, -_sz)
    # 同簇变体质量排名: 演唱版在前、伴奏/纯音乐垫后、同类按无损>码率>体积
    variant_rank = {}
    for _c, _idxs in cluster_members.items():
        for _r, _i in enumerate(sorted(_idxs, key=lambda i: _intra(raw_tracks[i]))):
            variant_rank[id(raw_tracks[_i])] = _r
    # 统一比较器: 用户置顶 -> 用户拖动序 -> 变体轮次(每首歌的最佳版本先出场, 同名变体不再扎堆,
    #             轮次内按簇首现序≈构建相关度) -> 簇内质量 -> 原始序兜底
    _orig_idx = {id(t): i for i, t in enumerate(raw_tracks)}
    raw_tracks.sort(key=lambda t: (
        _pin_idx.get(_tkey(t), 10**6),
        _ord_idx.get(_tkey(t), 10**6),
        variant_rank.get(id(t), 0),
        first_seen.get(_cluster_key(t.get('song_name')), 10**6),
    ) + _intra(t) + (_orig_idx[id(t)],))
    payload['tracks'] = raw_tracks
    tracks = []
    for i, t in enumerate(payload.get('tracks') or []):
        song_data = t.pop('_song', None) or {}
        with suppress(Exception):
            song = _SongInfo.fromdict(song_data)
            # 外层无链接字段时以 _song 为准; 空链接的坏条目不注册(播放会502)
            if song.download_url in (None, '', 'None'):
                continue
            items[f'Ethnos#{i}'] = song
            t['sid'] = sid
            if t.get('download_url') in (None, '', 'None'):
                t['download_url'] = song.download_url
                t['ext'] = t.get('ext') or str(song.ext or '').upper()
        t['id'] = f'Ethnos#{i}'   # 统一按索引重写id, 与session注册键保持一致
        tracks.append({k: v for k, v in t.items() if k != '_song'})
    payload['tracks'] = tracks
    payload['count'] = len(tracks)
    payload['platform'] = 'ethnos'
    payload['platform_name'] = ('专题音乐' if str(group_key).startswith('t') else ('汉族音乐' if group_key == 'han' else '民族音乐'))
    payload['id'] = group_key
    payload['sid'] = sid
    with ethnos_lock:
        ethnos_state[group_key] = {'state': 'done', 'count': len(tracks)}
    return payload


def ethnos_build_worker(group_key):
    build_ethnos_playlist(group_key)


def ethnos_build_all_worker():
    """顺序构建+适度节流(防音源限流导致空结果); 曲目过少的民族优先补建"""
    with ethnos_lock:
        pending = [g['key'] for g in ETHNIC_GROUPS if ethnos_state[g['key']]['state'] != 'done']
        # 优先补建曲目缺损最严重的民族
        pending.sort(key=lambda k: -(ethnos_state[k].get('count') if ethnos_state[k].get('count') else 999))

    def consume():
        while True:
            if ethnos_stop.is_set():
                return
            try:
                key = pending.pop(0)
            except IndexError:
                return
            ok = build_ethnos_playlist(key)
            # 空结果(限流征兆)则长间隔后再试下一个
            time.sleep(20 if not ok else 6)
    workers = [threading.Thread(target=consume, daemon=True)]
    [w.start() for w in workers]
    [w.join() for w in workers]


# 启动时恢复已构建的民族歌单状态 (schema版本不一致或曲目过少的缓存视为待重建)
ETHNOS_MIN_VIABLE = 100
for _g in ETHNIC_GROUPS:
    if (_cache_path(_g['key'])).exists():
        with suppress(Exception):
            _p = json.loads((_cache_path(_g['key'])).read_text(encoding='utf-8'))
            if _p.get('v') == ETHNOS_SCHEMA and (_p.get('count') or 0) >= ETHNOS_MIN_VIABLE:
                if _p.get('partial') and (_p.get('count') or 0) < ETHNOS_MAX_SONGS:
                    ethnos_state[_g['key']] = {'state': 'idle', 'count': _p.get('count') or 0}
                else:
                    ethnos_state[_g['key']] = {'state': 'done', 'count': _p.get('count') or 0}


def _iter_ethnos_payload():
    """遍历全部已构建民族歌单(含汉族民间小调), 产出 (group_name, payload)。
    需要读歌单级元数据(如 artists_removed 移除名单)时用这个, 避免为拿名单二次全量读盘。"""
    files = [(g['key'], _cache_path(g['key'])) for g in ETHNIC_GROUPS]
    files.append(('han', ETHNOS_CACHE_DIR / 'han_汉族民间小调.json'))
    for key, path in files:
        if not path.exists():
            continue
        with suppress(Exception):
            payload = json.loads(path.read_text(encoding='utf-8'))
            if payload.get('v') != ETHNOS_SCHEMA:
                continue
            yield payload.get('group') or key, payload


def _iter_ethnos_tracks():
    """遍历全部已构建民族歌单(含汉族民间小调), 产出 (group_name, track)"""
    for group, payload in _iter_ethnos_payload():
        for t in payload.get('tracks') or []:
            yield group, t


def _split_singers(s):
    """拆分多歌手: '唐木锐、进同干' -> ['唐木锐','进同干'] (合作曲同时归属每位歌手)"""
    import re as _re
    parts = [x.strip() for x in _re.split(r'[/,,、&+]', str(s or '')) if x.strip()]
    return [x for x in parts if x and x.upper() not in {'NULL', 'NONE', '未知歌手'}] or ([str(s or '').strip()] if str(s or '').strip() else [])


def _first_singer(s):
    return str(s or '').split('/')[0].split(',')[0].split('、')[0].strip()


def _index_virtual_playlist(kind, key):
    """虚拟歌单: artist=歌手名(穷尽式), album=专辑名@歌手名, ytcoll=景颇YouTube合集名"""
    from musicdl.modules import SongInfo as _SongInfo
    if kind == 'ytcoll':
        pool = [t for gname, t in _iter_ethnos_tracks() if t.get('album') == key and t.get('_song', {}).get('download_url')]
        if not pool:
            return None
        return _finalize_virtual_playlist(kind, key, f"合辑「{key}」", pool)
    if kind == 'album':
        album_name, artist = (key.split('@', 1) + [''])[:2]
        pool = []
        for gname, t in _iter_ethnos_tracks():
            singer = _first_singer(t.get('singers'))
            if _norm_str(t.get('album') or '') != _norm_str(album_name):
                continue
            if artist and _norm_str(singer) != _norm_str(artist):
                continue
            pool.append(t)
        if not pool:
            return None
        return _finalize_virtual_playlist(kind, key, f"专辑「{album_name}」", pool)
    # artist: 民族歌单聚合 [+ 歌手全网搜索穷尽, 仅在未限定民族时]
    #   key 形如 "歌手名" 或 "歌手名@民族名": 带民族时只收录该民族内的曲目,
    #   与索引卡片上的数字严格一致(此前不限民族, 丽江若杰在珞巴卡片显示65、点进去却是跨民族的237)
    artist_name, _, group_scope = key.partition('@')
    artist_name = artist_name or key
    group_scope = group_scope.strip()
    pool, seen = [], set()
    artist_key = _norm_str(artist_name)
    for gname, t in _iter_ethnos_tracks():
        if group_scope and _norm_str(gname) != _norm_str(group_scope):
            continue   # 限定了民族: 其他民族的曲目一律不收
        if not (t.get('_song', {}).get('download_url')):
            continue   # 无下载链接的杂歌(如国内源模糊匹配结果)不入虚拟歌单
        # 多歌手拆分: 任一歌手匹配即收录(合作曲同时出现在每位歌手的虚拟歌单)
        singer_parts = _split_singers(t.get('singers'))
        singer = _norm_str(singer_parts[0]) if singer_parts else ''
        matched_parts = [_norm_str(x) for x in singer_parts]
        matched = any((mp and mp == artist_key) or ((not group_scope) and ((len(mp) >= 3 and mp in artist_key) or (len(artist_key) >= 3 and artist_key in mp))) for mp in matched_parts)
        # 匹配规则: 精确相等; 未限定民族时才启用双向子串(要求短的一方长度>=3, 排除 'a'/'k' 等单字母UGC歌手误匹配)
        if matched:
            # 去重键带上曲目唯一 id(netease_id/identifier): 电台节目同名不同集(如重复的直播回放)
            # 不再被"歌名+歌手"误合并 —— 佤族电台 422 期曾因此被折叠成 411
            k = _norm_str(t.get('song_name')) + '|' + singer + '|' + str(t.get('netease_id') or (t.get('_song') or {}).get('identifier') or '')
            if k not in seen:
                seen.add(k)
                pool.append(t)
    spath = SINGER_CACHE_DIR / f'{_norm_path(artist_name)}.json'
    if not group_scope:
        # 全网穷尽仅在未限定民族时启用: 限定了民族就必须与卡片数字一致, 不再掺入搜索缓存
        if not spath.exists():
            with singer_lock:
                building = artist_name in singer_building
            if not building:
                threading.Thread(target=build_singer_playlist, args=(artist_name,), daemon=True).start()
            if not pool:
                return {'building': True, 'eta': 40}
        with suppress(Exception):
            sc = json.loads(spath.read_text(encoding='utf-8')) if spath.exists() else {}
            for t in sc.get('tracks') or []:
                if not (t.get('_song', {}).get('download_url')):
                    continue
                singer = _norm_str(_first_singer(t.get('singers')))
                matched = (singer and singer == artist_key) or (len(singer) >= 3 and singer in artist_key) or (len(artist_key) >= 3 and artist_key in singer)
                if not matched:
                    continue   # 搜索带回的无关结果(如其他民族歌曲)不入虚拟歌单
                k = _norm_str(t.get('song_name')) + '|' + singer
                if k not in seen:
                    seen.add(k)
                    pool.append(t)
    if not pool:
        return {'building': True, 'eta': 40}
    label = f"歌手「{artist_name}」" + (f" · {group_scope}" if group_scope else '')
    return _finalize_virtual_playlist(kind, key, label, pool)


def _finalize_virtual_playlist(kind, key, label, pool):
    """把聚合出的 track 池注册进可播放会话并生成歌单详情"""
    from musicdl.modules import SongInfo as _SongInfo
    client = _ethnos_get_client()
    sid = new_session(client)
    items = search_sessions[sid]['items']
    tracks = []
    for i, t in enumerate(pool[:2600]):
        song_data = t.get('_song') or {}
        with suppress(Exception):
            items[f'Ethnos#{i}'] = _SongInfo.fromdict(song_data)
            t = dict(t, sid=sid)
        t = dict(t, sid=sid, id=f'Ethnos#{i}')   # 统一按索引重写id, 与session注册键一致
        tracks.append({k: v for k, v in t.items() if k != '_song'})
    return {'id': f'{kind}:{key}', 'name': label, 'cover': (tracks[0].get('cover_url') if tracks else ''),
            'count': len(tracks), 'tracks': tracks, 'platform': 'ethnos', 'platform_name': f'民族音乐索引 · {label}',
            'sid': sid}
# 音源中文名 (未命中的音源直接显示原始名称)
SOURCE_NAMES = {
    'Migu': '咪咕', 'Netease': '网易云', 'QQ': 'QQ音乐', 'Kuwo': '酷我', 'Kugou': '酷狗',
    'Qianqian': '千千', 'Soda': '汽水', 'Bilibili': 'B站', 'Bodian': '波点', 'FiveSing': '5sing',
    'StreetVoice': '街声', 'MOOV': '摩音符', 'Apple': '苹果音乐', 'ITunes': 'iTunes', 'Spotify': 'Spotify',
    'SoundCloud': '声云', 'TIDAL': 'TIDAL', 'Qobuz': 'Qobuz', 'Deezer': 'Deezer', 'Jamendo': 'Jamendo',
    'JioSaavn': 'JioSaavn', 'Joox': 'JOOX', 'Suno': 'Suno', 'YouTube': 'YouTube', 'Ximalaya': '喜马拉雅',
    'Lizhi': '荔枝FM', 'Qingting': '蜻蜓FM', 'LRTS': 'LRTS', 'GDStudio': 'GD聚合', 'WikimediaCommons': '维基共享',
    'OpenGameArt': '游戏素材', 'FMA': 'FMA', 'CCMixter': 'CCMixter', 'Audius': 'Audius', 'MyFreeMP3': 'MyFreeMP3',
    'MP3Juice': 'MP3Juice', 'TwoT58': 'TwoT58', 'XMFWAV': 'XMFWAV', 'Gequhai': '歌曲海', 'Sgogo': 'Sgogo',
    'JBSou': '聚爆搜', 'Yinyueku': '音乐库', 'XiaoBai': '小白聚合', 'Weixin': '微信公众号',
}

mimetypes.add_type('audio/flac', '.flac')
mimetypes.add_type('audio/mp4', '.m4a')
mimetypes.add_type('audio/ogg', '.ogg')

app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent / 'templates'))
app.config['JSON_AS_ASCII'] = False

state_lock = threading.Lock()
clients_cache = {}
search_sessions = OrderedDict()   # sid -> {'client', 'items': {'Source#idx': SongInfo}}
search_jobs = OrderedDict()       # job_id -> 渐进式搜索任务
tasks = OrderedDict()             # tid -> 下载任务
playlist_cache = {}               # netease歌单id -> {'data', 'at'}
download_pool = ThreadPoolExecutor(max_workers=3)


# ---------------------------------------------------------------- 基础工具
def get_client(sources):
    key = frozenset(sources)
    with state_lock:
        if key not in clients_cache:
            cfg = {s: {'work_dir': str(DOWNLOAD_DIR), 'search_size_per_source': SEARCH_SIZE_PER_SOURCE, 'max_retries': 1} for s in sources}
            clients_cache[key] = musicdl.MusicClient(music_sources=list(sources), init_music_clients_cfg=cfg)
        return clients_cache[key]


def new_session(client, items=None):
    sid = uuid.uuid4().hex[:12]
    with state_lock:
        search_sessions[sid] = {'client': client, 'items': items if items is not None else {}}
        while len(search_sessions) > 16:
            search_sessions.popitem(last=False)
    return sid


def session_item(sid, item_id):
    with state_lock:
        session = search_sessions.get(sid)
    return session['items'].get(item_id) if session else None


def _ext_album_url(song):
    """专辑外链(搜索结果点击专辑列 -> 新标签打开原平台页):
    有平台专辑 id 用精确主页, 拿不到就落到该平台的搜索页 —— 保证「一定有外链」。"""
    with suppress(Exception):
        from urllib.parse import quote as _quote
        raw = (song.raw_data or {}).get('search', {}) or {}
        src = str(song.source or '').removesuffix('MusicClient')
        if src == 'Netease':
            aid = (raw.get('album') or {}).get('id') if isinstance(raw.get('album'), dict) else raw.get('albumId')
            if aid:
                return f'https://music.163.com/#/album?id={aid}'
        if src == 'Kuwo':
            aid = raw.get('ALBUMID') or raw.get('album_id')
            if aid:
                return f'https://www.kuwo.cn/album_detail/{aid}'
        if src == 'Migu':
            aid = raw.get('albumId') or raw.get('album_id')
            if aid:
                return f'https://music.migu.cn/v3/music/album/{aid}'
        q = str(song.album or '').strip()
        tpl = {
            'Netease': 'https://music.163.com/#/search/?s={q}',
            'Kuwo': 'https://www.kuwo.cn/search_list?key={q}',
            'Kugou': 'https://www.kugou.com/ss/?keyword={q}',
            'Migu': 'https://music.migu.cn/v3/search?keyword={q}',
            'QQ': 'https://y.qq.com/n/ryqq/search?w={q}',
            'Bilibili': 'https://search.bilibili.com/all?keyword={q}',
            'Weixin': 'https://weixin.sogou.com/weixin?type=2&query={q}',
        }.get(src)
        if tpl and q and q != '—':
            return tpl.format(q=_quote(q))
    return None


def song_brief(song, source, idx):
    _dl = song.download_url
    # ytdlp: 通道实测(2026-09-28): bilibili 可实时解析出有效 CDN 直链; youtube 在本机网络下
    # SSL 握手被阻断(UNEXPECTED_EOF_WHILE_READING), 放开只会让用户点了白等, 故区别对待
    _bili_pipe = (isinstance(_dl, str) and _dl.startswith('ytdlp:') and 'bilibili.com' in _dl)
    previewable = song.protocol == 'HTTP' and isinstance(_dl, str) and (_dl.startswith('http') or _bili_pipe)

    def clean(v):
        v = str(v).strip() if v is not None else ''
        return v if v and v.upper() not in {'NULL', 'NONE'} else ''
    return {
        'id': f'{source}#{idx}',
        'song_name': clean(song.song_name) or '未知曲目',
        'singers': clean(song.singers) or '未知歌手',
        'album': clean(song.album),
        'ext': (song.ext or '').lstrip('.').upper(),
        'file_size': clean(song.file_size),
        'duration': clean(song.duration),
        'duration_s': song.duration_s,
        'bitrate': song.bitrate,
        'cover_url': clean(song.cover_url),
        'lyric': bool(clean(song.lyric)),
        'source': source.removesuffix('MusicClient'),
        'source_cn': SOURCE_NAMES.get(source.removesuffix('MusicClient'), source.removesuffix('MusicClient')),
        'previewable': bool(previewable),
        'downloadable': bool(previewable or song.with_valid_download_url),
        'album_url': _ext_album_url(song),
    }


def download_worker(tid, client, song):
    with state_lock:
        tasks[tid]['status'] = 'downloading'
    try:
        # YouTube 直链曲目: 实时提取音频流后直接下载写盘
        if isinstance(song.download_url, str) and song.download_url.startswith('ytdlp:'):
            real_url = _resolve_ytdlp_url(song.download_url)
            if not real_url:
                raise Exception('yt-dlp 提取音频链接失败')
            dl_headers = {'user-agent': UA}
            if 'bilibili' in real_url or 'bilivideo' in real_url:
                dl_headers['Referer'] = 'https://www.bilibili.com/'   # B站CDN校验Referer
            import re as _re2
            from pathvalidate import sanitize_filepath
            safe_stem = _re2.sub(r'[\\/:*?"<>|\r\n]+', ' ', f"{song.song_name[:60]} - {song.identifier}").strip()
            fname = sanitize_filepath(os.path.join(str(DOWNLOAD_DIR), f"{safe_stem}.{(song.ext or 'webm').lstrip('.')}"))
            # googlevideo 长连接易中断: 断点续传+重试
            offset, attempts = 0, 0
            while attempts < 5:
                headers = dict(dl_headers)
                if offset:
                    headers['Range'] = f'bytes={offset}-'
                try:
                    with requests.get(real_url, headers=headers, stream=True, timeout=(10, 120)) as up, open(fname, 'ab' if offset else 'wb') as f:
                        for chunk in up.iter_content(chunk_size=1024 * 512):
                            if chunk:
                                f.write(chunk)
                                offset += len(chunk)
                    if offset > 0:
                        break
                    raise Exception('empty download')
                except Exception:
                    attempts += 1
                    time.sleep(2)
                    if attempts >= 5:
                        raise
            with state_lock:
                tasks[tid].update(status='done', file=os.path.relpath(fname, DOWNLOAD_DIR), finished_at=time.time())
            return
        # 微信视频直链带签名时效(约2天)/腾讯视频vkey时效更短: 下载前回源刷新, 避免直接撞过期链
        if isinstance(song.download_url, str) and ('mpvideo.qpic.cn' in song.download_url or '.gtimg.com/' in song.download_url):
            with suppress(Exception):
                _refresh_weixin_link(song)
        if song.source not in getattr(client, 'music_clients', {}):
            # 会话客户端不含该音源(民族库缓存的微信公众号曲目即如此, ethnos 客户端无 Weixin):
            # 直链直接流式落盘, 与下方 client.download 等价; 缺了这步会 KeyError -> 误报下载失败
            import re as _re3
            from pathvalidate import sanitize_filepath
            dl_headers = dict(song.default_download_headers or {})
            dl_headers.setdefault('User-Agent', UA)
            if isinstance(song.download_url, str) and ('mpvideo.qpic.cn' in song.download_url or 'res.wx.qq.com' in song.download_url):
                dl_headers.setdefault('Referer', 'https://mp.weixin.qq.com/')
            elif isinstance(song.download_url, str) and '.gtimg.com/' in song.download_url:
                dl_headers['Referer'] = 'https://v.qq.com/'
            safe_stem3 = _re3.sub(r'[\\/:*?"<>|\r\n]+', ' ', f"{song.song_name[:60]} - {song.identifier}").strip()
            fname3 = sanitize_filepath(os.path.join(str(DOWNLOAD_DIR), f"{safe_stem3}.{(song.ext or 'mp4').lstrip('.')}"))
            offset3, attempts3 = 0, 0
            while attempts3 < 5:
                headers3 = dict(dl_headers)
                if offset3:
                    headers3['Range'] = f'bytes={offset3}-'
                try:
                    with requests.get(song.download_url, headers=headers3, stream=True, timeout=(10, 120)) as up3, open(fname3, 'ab' if offset3 else 'wb') as f3:
                        for chunk3 in up3.iter_content(chunk_size=1024 * 512):
                            if chunk3:
                                f3.write(chunk3)
                                offset3 += len(chunk3)
                    if offset3 > 0:
                        break
                    raise Exception('empty download')
                except Exception:
                    attempts3 += 1
                    time.sleep(2)
                    if attempts3 >= 5:
                        raise
            with state_lock:
                tasks[tid].update(status='done', file=os.path.relpath(fname3, DOWNLOAD_DIR), finished_at=time.time())
            return
        result = client.download([song])
        spath = result[0].save_path if result else None
        if spath and os.path.exists(spath):
            with state_lock:
                tasks[tid].update(status='done', file=os.path.relpath(spath, DOWNLOAD_DIR), finished_at=time.time())
        else:
            with state_lock:
                tasks[tid].update(status='failed', error='下载失败(可能无版权或链接失效)', finished_at=time.time())
    except Exception as err:
        with state_lock:
            tasks[tid].update(status='failed', error=str(err)[:200], finished_at=time.time())


def source_search_worker(job_id, source):
    job = search_jobs[job_id]
    try:
        songs = job['client'].music_clients[source].search(keyword=job['keyword'], num_threadings=5, request_overrides={}, rule={}) or []
    except Exception as err:
        # 不再静默吞掉异常: 记录到 job 里, 由 /api/search/status 带回前端,
        # 否则音源失效时用户只看到"没有结果", 无从判断是没搜到还是源挂了。
        songs = []
        with state_lock:
            job.setdefault('errors', {})[source] = str(err)[:200]
    with state_lock:
        job['results'][source] = songs
        job['done'][source] = True
        for i, s in enumerate(songs):
            job['items'][f'{source}#{i}'] = s


# ---------------------------------------------------------------- 页面
@app.route('/')
def index():
    return render_template('mountainriverechoes.html')


# ---------------------------------------------------------------- 搜索 (渐进式, 逐源返回)
@app.route('/api/sources')
def api_sources():
    all_sources = sorted(k.removesuffix('MusicClient') for k in MusicClientBuilder.REGISTERED_MODULES.keys())
    return jsonify({
        'default': [s.removesuffix('MusicClient') for s in DEFAULT_SOURCES],
        'fast': FAST_SOURCES,
        'all': all_sources,
        'names': SOURCE_NAMES,
    })


@app.route('/api/search', methods=['POST'])
def api_search():
    data = request.get_json(force=True)
    keyword = (data.get('keyword') or '').strip()
    sources = [s for s in (data.get('sources') or DEFAULT_SOURCES) if s]
    if not keyword:
        return jsonify({'error': '请输入搜索关键词'}), 400
    touch_user_activity()
    sources = [s if s.endswith('MusicClient') else f'{s}MusicClient' for s in sources]
    client = get_client(sources)
    sid = new_session(client)
    job_id = uuid.uuid4().hex[:12]
    with state_lock:
        search_jobs[job_id] = {
            'client': client, 'keyword': keyword, 'sources': list(sources),
            'done': {s: False for s in sources}, 'results': {}, 'items': search_sessions[sid]['items'],
            'sid': sid, 'created_at': time.time(),
        }
        while len(search_jobs) > 20:
            search_jobs.popitem(last=False)
    for s in sources:
        threading.Thread(target=source_search_worker, args=(job_id, s), daemon=True).start()
    return jsonify({'job_id': job_id, 'sid': sid, 'sources': [s.removesuffix('MusicClient') for s in sources]})


@app.route('/api/search/status')
def api_search_status():
    job_id = request.args.get('job')
    touch_user_activity()
    with state_lock:
        job = search_jobs.get(job_id)
        if job is None:
            return jsonify({'error': '搜索任务不存在或已过期'}), 404
        # 90秒兜底: 挂起的音源强制标记完成, 避免前端无限"仍在搜索"
        expired = time.time() - job['created_at'] > 90
        if expired:
            for s in job['sources']:
                job['done'][s] = True
        groups = [
            {'source': src.removesuffix('MusicClient'), 'source_cn': SOURCE_NAMES.get(src.removesuffix('MusicClient'), src),
             'done': job['done'][src],
             'error': (job.get('errors') or {}).get(src),
             'items': [song_brief(s, src, i) for i, s in enumerate(job['results'].get(src, []))]}
            for src in job['sources']
        ]
        snapshot = {
            'sid': job['sid'], 'keyword': job['keyword'], 'groups': groups,
            'total': sum(len(g['items']) for g in groups),
            'finished': all(job['done'].values()),
        }
        if snapshot['finished'] and time.time() - job['created_at'] > 900:
            search_jobs.pop(job_id, None)
    return jsonify(snapshot)


# ---------------------------------------------------------------- 试听 / 封面 / 歌词
def _resolve_ytdlp_url(fake_url):
    """'ytdlp:<watch_url>' -> 实时提取音频直链 (googlevideo 约6小时过期; B站部分链接的oi非本机IP形态会403, 遇则重试)
    YouTube 2026 起强反爬: 无登录态必被 bot-gate, 需借 Chrome cookies + EJS 挑战求解组件"""
    if not (isinstance(fake_url, str) and fake_url.startswith('ytdlp:')):
        return fake_url
    watch = fake_url[6:]
    is_yt = 'youtube.com' in watch or 'youtu.be' in watch
    for attempt in range(4):
        try:
            ytdlp_bin = _ytdlp_bin()
            cmd = [ytdlp_bin, '--no-update', '-f', 'bestaudio/best', '--get-url', watch]
            if is_yt:
                # YouTube 2026 强反爬: 借 Chrome 登录态 + EJS 求解组件(ejs脚本有本地缓存, 仅首次下载)
                cmd = [ytdlp_bin, '--no-update', '--cookies-from-browser', 'chrome', '--remote-components', 'ejs:github',
                       '-f', 'bestaudio/best', '--get-url', watch]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            lines = [x.strip() for x in (result.stdout or '').splitlines() if x.strip().startswith('http')]
            if lines:
                # 实测纯数字oi与0x形态均可正常取流(206), 不做形态过滤
                return lines[-1]
        except Exception:
            pass
    return None


def _bili_watch_url(song):
    """B站曲目取原始视频页: ytdlp:前缀直接剥; 存量CDN直链已过期, 从raw_data递归找回视频页/BV号"""
    u = str(song.download_url or '')
    if u.startswith('ytdlp:'):
        return u[6:]
    if 'bilibili.com/video' in u or 'b23.tv' in u:
        return u

    def dig(o, depth=0):
        if depth > 5:
            return None
        if isinstance(o, str):
            if 'bilibili.com/video' in o or 'b23.tv' in o:
                return o
            if re.fullmatch(r'BV[0-9A-Za-z]{10}', o):
                return f'https://www.bilibili.com/video/{o}'
            return None
        if isinstance(o, dict):
            for v in o.values():
                r = dig(v, depth + 1)
                if r:
                    return r
        if isinstance(o, list):
            for v in o[:10]:
                r = dig(v, depth + 1)
                if r:
                    return r
        return None
    return dig(getattr(song, 'raw_data', None) or {})


def _weixin_video_self_heal(article_url, voice_id):
    """单独拿文章页 + 解析 mpvideo 直链, 返回 (url, requests.Session)。session 给流转发用, 避免签名被裸 requests 拿走。

    mp_video_trans_info 里的 dis_k/dis_t 是绑 poc_sid 会话级的, 用别的会话去拉必 403。
    """
    sess = requests.Session()
    sess.headers.update({'User-Agent': UA, 'Referer': 'https://mp.weixin.qq.com/'})
    try:
        r = sess.get(article_url.replace('http://', 'https://'), timeout=(5, 25))
        body = r.text or ''
    except Exception:
        return None, None
    if len(body) < 60000:
        return None, None
    # 原生音频: 直接返回永久链, 不用 session
    if voice_id and re.search(r'voice_encode_fileid="%s"' % re.escape(voice_id), body):
        return f'https://res.wx.qq.com/voice/getvoice?mediaid={voice_id}', None
    groups = {}
    for m in re.finditer(r"format_id:\s*'(\d+)'[^{}]*?url:\s*'(http://mpvideo[^']+)'", body, re.S):
        fmt, raw = m.group(1), m.group(2)
        base = re.match(r'https?://mpvideo\.qpic\.cn/([^/?]+)\.', raw)
        if not base:
            continue
        url = raw.replace('\\x26amp;', '&').replace('&amp;', '&')
        groups.setdefault(base.group(1), {})[fmt] = url
    # H.264(f1000x) 优先
    for fmt in ('10004', '10002', '10104', '10102'):
        for base in sorted(groups):
            u = groups[base].get(fmt)
            if u:
                return u, sess
    return None, None


def _refresh_weixin_link(song):
    """微信文章曲目直链自愈: mpvideo 视频直链带签名时效(约2天), 过期后回文章页重取最新签名直链。
    专辑入库的条目 raw_data.wechat 自带文章 URL 与 voice_id, 音频(getvoice)与视频(mpvideo)都能重取。
    腾讯视频合集条目的 wechat.url 是 v.qq.com 合集页(不是文章), 但 voice_id 带 qqvid- 前缀 ——
    直接走 getinfo 重签 fvkey。此前被开头的 mp.weixin 域名校验挡住, 景颇 195 条合集条目 403 无法自愈。"""
    wechat = (((getattr(song, 'raw_data', None) or {}).get('wechat')) or {})
    if not isinstance(wechat, dict):
        wechat = {}
    # 线索兜底(两处历史形态, 都补造 raw_data.wechat 走同一条自愈路):
    #   1) identifier 带 qqvid-/qqmid- 前缀却没建 raw_data.wechat
    #   2) 文章 URL 落在条目顶层(article_url/voice_id/article)而没下沉到 _song.raw_data
    if not (wechat.get('voice_id') or wechat.get('url')):
        _ident = str(getattr(song, 'identifier', '') or '')
        if _ident.startswith(('qqvid-', 'qqmid-')):
            wechat = {'voice_id': _ident, 'url': '', 'article': ''}
        else:
            top_url = str(getattr(song, 'article_url', '') or '')
            top_vid = str(getattr(song, 'voice_id', '') or '')
            if not top_url or 'mp.weixin.qq.com' not in top_url:
                # SongInfo 认不得顶层自定义字段, 入库时可能整个塞在 raw_data 里
                _raw = getattr(song, 'raw_data', None) or {}
                if isinstance(_raw, dict):
                    top_url = top_url or str(_raw.get('article_url') or '')
                    top_vid = top_vid or str(_raw.get('voice_id') or '')
            if top_url:
                wechat = {'voice_id': top_vid, 'url': top_url, 'article': ''}
    vid0 = str(wechat.get('voice_id') or '').strip()
    if vid0.startswith('qqmid-'):
        fresh = _qqmusic_reissue_url(song, vid0[len('qqmid-'):])
        if fresh:
            return fresh
    if vid0.startswith('qqvid-'):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from wx_video_album import qqvideourl as _qq
        with suppress(Exception):
            parsed = _qq(vid0[len('qqvid-'):])
            if parsed:
                song.download_url = parsed[0]
                return parsed[0]
    article_url = wechat.get('url') or ''
    if not isinstance(article_url, str) or 'mp.weixin.qq.com' not in article_url:
        return None
    # mpvideo 走 helper: helper 同时返回会话, 交给 api_preview 转发, 否则签名失效全 403
    fresh_url, sess = _weixin_video_self_heal(article_url, wechat.get('voice_id') or '')
    if fresh_url:
        song.download_url = fresh_url
        if sess is not None:
            with suppress(Exception):
                song._wx_sess = sess
        return fresh_url
    # 老形态: 正文只挂腾讯视频 vid(video_ids), 走 getinfo 重取 fvkey 直链
    vid = str(wechat.get('voice_id') or '')
    if vid.startswith('qqvid-'):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from wx_video_album import qqvideourl as _qq
        with suppress(Exception):
            parsed = _qq(vid[len('qqvid-'):])
            if parsed:
                song.download_url = parsed[0]
                return parsed[0]
    return None


_QQ_PLAY_FIELDS = (   # 优先级: m4a 系列(浏览器/解码器兼容最好) > flac > 低码率 m4a
    ('song_play_url_hq', 'm4a'), ('song_play_url', 'm4a'), ('song_play_url_sq', 'flac'),
    ('song_play_url_standard', 'm4a'), ('song_play_url_fq', 'm4a'),
)


def _qqmusic_link_by_mid(mid):
    """mid -> 新鲜直链。官方 musicu 的 vkey 接口对匿名请求已不返回 purl, api.vkeys.cn 也要 key 了,
    所以按可用性依次试解析源: 每个源返回的都是内嵌 vkey 的短效链, 但每次调用都重新签发,
    等效于「随用随续」。拿到链当场 Range 试听验活, 只认 200/206, 免得把别的失效链又写回去。"""
    h = {'user-agent': UA, 'Referer': 'https://y.qq.com/'}
    cands = []
    with suppress(Exception):
        j = requests.get(f'https://api.hk0.cc/api/qqmusic?mid={mid}', headers=h, timeout=(5, 15), verify=False).json()
        cands += [j.get(k) for k, _ in _QQ_PLAY_FIELDS]
    with suppress(Exception):
        j = requests.get(f'https://tang.api.s01s.cn/music_open_api.php?mid={mid}', headers=h, timeout=(5, 15)).json()
        cands += [j.get(k) for k, _ in _QQ_PLAY_FIELDS]
    with suppress(Exception):
        cands.append(requests.get(f'https://api.baka.plus/meting/?server=tencent&type=url&id={mid}&br=2000',
                                  headers=h, timeout=(5, 15), allow_redirects=True, stream=True).url)
    seen = set()
    for u in cands:
        u = str(u or '').strip()
        if not u.startswith('http') or u in seen:
            continue
        seen.add(u)
        try:
            r = requests.get(u, headers={**h, 'Range': 'bytes=0-1023'}, stream=True, timeout=(5, 12))
            ok = r.status_code in (200, 206)
            r.close()
        except Exception:
            ok = False
        if ok:
            return u
    return ''


def _qqmusic_reissue_url(song, mid=None):
    """QQ音乐直链续签: ws.stream.qqmusic.qq.com 链接内嵌 vkey, 几小时即废(文章卡入库当天就播不出)。

    续签是**精确到曲**的(按 songmid), 不存在重搜那样的同名顶替风险, 所以优先于跨源重搜。
    mid 来源: identifier(qqmid-xxx) > 直链文件名(C200+mid.m4a / M500+mid.mp3 / F000+mid.flac)。
    """
    if not mid:
        ident = str(getattr(song, 'identifier', '') or '').strip()
        if ident.startswith('qqmid-'):
            mid = ident[len('qqmid-'):]
        else:
            m = re.search(r'/[A-Z]\d{3}([A-Za-z0-9]{10,16})\.(?:m4a|mp3|flac|ape|ogg|wav)', str(getattr(song, 'download_url', '') or ''))
            mid = m.group(1) if m else (ident if re.fullmatch(r'[A-Za-z0-9]{14}', ident) else '')
    if not mid:
        return None
    with suppress(Exception):   # 当前直链自己就还活着的话(未过期), 原样返回, 少绕一圈第三方
        cur = str(getattr(song, 'download_url', '') or '')
        if 'ws.stream.qqmusic.qq.com' in cur or 'isure' in cur:
            r = requests.get(cur, headers={'user-agent': UA, 'Referer': 'https://y.qq.com/', 'Range': 'bytes=0-1023'},
                             stream=True, timeout=(5, 10))
            ok = r.status_code in (200, 206)
            r.close()
            if ok:
                return cur
    fresh = ''
    with suppress(Exception):
        fresh = _qqmusic_link_by_mid(mid)
    if not str(fresh).startswith('http'):
        return None
    song.download_url = fresh
    if not str(getattr(song, 'identifier', '') or '').startswith('qqmid-'):
        with suppress(Exception):
            song.identifier = f'qqmid-{mid}'   # 记下 mid, 下次续签不必再从 URL 里抠
    return fresh


def _netease_reissue_url(song):
    """网易云直链续签: 电台/DJ音频与部分歌曲的 download_url 内嵌签发时间戳, 约40分钟即 403。
    会话对象以 identifier 保存网易云歌曲 id —— 直接调官方 URL API 重新签发, 优先于跨源重搜。"""
    with suppress(Exception):
        if str(song.source or '').removesuffix('MusicClient') != 'Netease':
            return None
        nid = str(getattr(song, 'identifier', '') or '')
        if not nid.isdigit():
            return None
        r = requests.get('https://music.163.com/api/song/enhance/player/url',
                         params={'ids': f'[{nid}]', 'br': 320000},
                         headers={'User-Agent': UA, 'Referer': 'https://music.163.com/'}, timeout=(5, 10))
        d = (r.json().get('data') or [{}])[0]
        if d.get('url'):
            with suppress(Exception):
                song.download_url = d['url']       # 回写会话对象, 本会话内后续播放不再重复续签
            return d['url']
    return None


def _kuwo_reissue_url(song):
    """酷我直链续签: 库内曲目带 rid(identifier), 直接调官方 convert_url2 接口按 id 重签 ——
    不依赖检索排名。小众民族曲目在搜索接口里搜不出来时(2026-10-03 藏族天杵乐队 49 首直链
    集体 410, 四源重搜均 0 结果), 这条按 id 重签的路仍然必中, 且秒级完成。"""
    with suppress(Exception):
        if str(song.source or '').removesuffix('MusicClient') != 'Kuwo':
            return None
        rid = str(getattr(song, 'identifier', '') or '')
        if not rid.isdigit():
            return None
        mc = _ethnos_get_client().music_clients.get('KuwoMusicClient')
        if mc is None:
            return None
        fresh = mc._parsewithofficialapiv1({'MUSICRID': f'MUSIC_{rid}'})
        url = getattr(fresh, 'download_url', None)
        if isinstance(url, str) and url.startswith('http'):
            song.download_url = url
            if fresh.ext:
                song.ext = fresh.ext
                song.file_size = fresh.file_size or song.file_size
            return url
    return None


_TITLE_STRIP_RE = re.compile(r"[\s\-—－·・,，.。'\"‘’`~～!！?？:：;；()（）\[\]【】{}<>《》&^%$#@*+=|\\/]+")


def _title_key(s):
    """歌名/歌手归一化键。剥离规则与前端 norm() 完全一致, 保证前后端判定同一套口径。"""
    return _TITLE_STRIP_RE.sub('', str(s or '').lower())


def _same_recording(tname, tsingers, cname, csingers):
    """判定两条记录是否**同一首歌**，只有同一首歌才允许互相顶替直链。

    判据(必须同时满足):
      1. 归一化歌名完全相同 —— 绝不做子串/相似度匹配;
      2. 歌手相容 —— 归一化歌手名相等, 或一方为空, 或长的一方包含短的一方(短方>=2字)。

    背景(2026-10-02 实测事故): 此前用 `cn == wn or wn in cn or cn in wn` 的子串匹配顶替直链,
    库内景颇族《你的微笑》(岳木果) 被飞儿乐团同名曲顶替、《景颇山我的家乡》被《我的家乡》顶替、
    《阿努的摇篮曲》被 QQ音乐《摇篮曲》(黑鸭子) 顶替 —— 显示的是库内歌名, 放出来的却是通俗同名曲。
    """
    tn, cn = _title_key(tname), _title_key(cname)
    if not tn or not cn or tn != cn:
        return False
    na, nb = _title_key(tsingers), _title_key(csingers)
    if not na or not nb or na == nb:
        return True
    return (len(na) >= 2 and na in nb) or (len(nb) >= 2 and nb in na)


def _search_with_timeout(client, src, keyword, timeout):
    """带硬超时的多源搜索。musicdl 的 search 内部会并发打多个第三方接口, 个别源卡住能拖到几分钟 ——
    试听转发是同步的, 必须能掐断, 否则前端一直转圈。"""
    box = []
    th = threading.Thread(target=lambda: box.extend(_safe_search(client, src, keyword)), daemon=True)
    th.start()
    th.join(timeout)
    return box


def _safe_search(client, src, keyword):
    try:
        return client.music_clients[src].search(keyword=keyword, num_threadings=2, request_overrides={}, rule={}) or []
    except Exception:
        return []


def _refresh_song_link(song):
    """直链过期自愈(酷我/酷狗CDN链接均带签名时效, 数小时~数天即410/403): 重搜同名曲换稳定直链并更新会话对象

    只接受「同一首歌」(歌名完全相同 + 歌手相容) 的候选; 搜不到就返回 None 让上层如实报错,
    宁可播不出也不能拿一首同名通俗曲冒充。"""
    if not song or not song.song_name:
        return None
    # 微信文章曲目优先回源重取(独家现场内容重搜大概率搜不到, 回源/续签必中)
    _u = song.download_url if isinstance(song.download_url, str) else ''
    if 'ws.stream.qqmusic.qq.com' in _u or str(getattr(song, 'identifier', '') or '').startswith('qqmid-'):
        # 文章里的 <qqmusic> 卡片: 直链带 vkey 时效, 按 mid 找第三方接口重签(比跨源重搜精确)
        with suppress(Exception):
            fresh = _qqmusic_reissue_url(song)
            if fresh:
                return fresh
    if ('mpvideo.qpic.cn' in _u or 'res.wx.qq.com' in _u or '.gtimg.com/' in _u
            or str(getattr(song, 'identifier', '') or '').startswith('qqvid-')):
        with suppress(Exception):
            fresh = _refresh_weixin_link(song)
            if fresh:
                return fresh
    with suppress(Exception):
        # 酷我: 库内存有 rid, 按 id 重签比跨源重搜又快又准(秒级 vs 28s 兜底重搜)
        fresh = _kuwo_reissue_url(song)
        if fresh:
            return fresh
    first_singer = str(song.singers or '').split('/')[0].split(',')[0].split('、')[0].strip()
    # 专辑列形如「真歌手 - 歌名」的合集条目(微信文章QQ卡片), singers 是合集名、真歌手在专辑列 ——
    # 这类条目只能用真歌手去搜/去比对, 否则歌手相容判据必然落空。
    alb_singer = ''
    _alb = str(getattr(song, 'album', '') or '')
    if ' - ' in _alb:
        _s = _alb.split(' - ')[0].strip()
        if _s and _norm_str(_s) != _norm_str(first_singer):
            alb_singer = _s
    expect = [s for s in (first_singer, alb_singer) if s]
    keywords = [f'{s} {song.song_name}' for s in expect] + [str(song.song_name)]
    best, best_sc = None, -1
    client = _ethnos_get_client()
    # B站优先(2026-10-03 调序, 原为四源之后的兜底): 民族音乐内容 B站收录最全, 候选是 ytdlp:
    # 形态——播放时实时解析、永不过期, 不像官方源重签的直链几天又烂; 搜索也远快于四源轮询。
    # 判据从严: 视频标题须**同时**包含歌名与歌手(B站标题是复合形态, 精确相等判据天然不适用),
    # 且只接受 ytdlp: 形态的候选。命中即定, 不再进入下面的四源重搜。
    with suppress(Exception):
        for kw in keywords[:2]:
            try:
                bsongs = _search_with_timeout(client, 'BilibiliMusicClient', kw, 10)
            except Exception:
                bsongs = []
            for cand in bsongs:
                tkey = _norm_str(str(getattr(cand, 'song_name', '') or ''))
                if not tkey or not _norm_str(song.song_name):
                    continue
                if (_norm_str(song.song_name) in tkey
                        and any(_norm_str(s) and _norm_str(s) in tkey for s in expect)
                        and isinstance(cand.download_url, str) and cand.download_url.startswith('ytdlp:')):
                    best, best_sc = cand, 130
                    break
            if best_sc >= 130:
                break
    deadline = time.time() + 28   # 试听是同步转发, 自愈不能把浏览器吊死; 超时就用已搜到的最好候选
    try:
        for kw in (keywords if best is None else []):   # B站已命中则跳过四源轮询
            for src in ('KugouMusicClient', 'KuwoMusicClient', 'MiguMusicClient', 'NeteaseMusicClient'):
                if time.time() > deadline:
                    break
                try:
                    songs = _search_with_timeout(client, src, kw, 12)
                except Exception:
                    songs = []
                for cand in songs:
                    if not cand.song_name:
                        continue
                    if not any(_same_recording(song.song_name, s, cand.song_name, cand.singers) for s in expect) \
                            and not _same_recording(song.song_name, song.singers, cand.song_name, cand.singers):
                        continue   # 歌名必须完全相同且歌手相容, 杜绝"同名不同歌"顶替
                    sc = 100
                    for i, s in enumerate(expect):
                        if _norm_str(s) and _norm_str(s) in _norm_str(cand.singers or ''):
                            sc += 30 if i == 0 else 20
                            break
                    if isinstance(cand.download_url, str) and cand.download_url.startswith('http') and cand.protocol == 'HTTP':
                        sc += 20
                    if sc > best_sc:
                        best, best_sc = cand, sc
                if best_sc >= 130:
                    break
            if best_sc >= 130 or time.time() > deadline:
                break
    except Exception:
        return None
    if best is None or not (isinstance(best.download_url, str) and best.download_url.startswith(('http', 'ytdlp:'))):
        return None
    song.download_url = best.download_url
    song.ext = best.ext or song.ext
    song.file_size = best.file_size or song.file_size
    song.duration = best.duration or song.duration
    song.source = best.source or song.source
    # 换源后 identifier 必须跟着换: 网易云的 40 分钟续签靠它, 留着旧哈希会让续签彻底失效
    if best.identifier:
        song.identifier = best.identifier
    return best.download_url


_LIB_IDX = {'sig': None, 'map': None}


def _library_identity_index():
    """(歌名, 歌手, 音源) -> (曲库文件 key, tracks 下标) 的惰性索引。

    全量扫盘约 1.9s, 所以只在曲库文件签名变化时重建(`_ethnos_files_sig()` 是 stat 级, 毫秒);
    索引里只放定位坐标、不放 `_song`, 避免 5 万条曲目数据常驻内存。命中后只读那一个 JSON。
    """
    sig = _ethnos_files_sig()
    if _LIB_IDX['sig'] == sig and _LIB_IDX['map'] is not None:
        return _LIB_IDX['map']
    files = [(g['key'], _cache_path(g['key'])) for g in ETHNIC_GROUPS]
    files.append(('han', ETHNOS_CACHE_DIR / 'han_汉族民间小调.json'))
    idx = {}
    for key, path in files:
        if not path.exists():
            continue
        with suppress(Exception):
            payload = json.loads(path.read_text(encoding='utf-8'))
            if payload.get('v') != ETHNOS_SCHEMA:
                continue
            for i, t in enumerate(payload.get('tracks') or []):
                if not (t.get('_song') or {}).get('download_url'):
                    continue
                k = (str(t.get('song_name') or '').strip(), str(t.get('singers') or '').strip(),
                     str(t.get('source') or '').strip().removesuffix('MusicClient'))
                idx.setdefault(k, (key, i))
    _LIB_IDX['sig'], _LIB_IDX['map'] = sig, idx
    return idx


# ============ 过期签名链后台重抓 ============
# Kugou/Kuwo/Netease/QQ/Migu 的 CDN 直链带签名时效(数小时~数天), 每次启动服务时,
# 后台并发重抓一批过期链(按 last_refresh_at 阈值 + 「最近播放」优先级)写回库, 避免每次
# 用户打开网页播放老歌都走 _refresh_song_link 同步阻塞(28s+ 超时)。
#
# [2026-10-03 用户决议: 关掉]
# 实测: 5 分钟跑 500 首, 0 首重抓成功。原因 _same_recording 安全阀(2026-10-02
# 定型)太严 —— 长尾/罕见曲目搜不到"歌名+歌手完全一致"的候选, 都被过滤掉;
# 陇川情这类搜狗本身找不到真身。worker 跑得再多, 绕过安全阀的概率极低。
# 改回"每次启动就开网页"的清爽状态: 用户听歌时, 能响的就响, 不能响的
# 前端直接提示"链接失效", 不再后台偷偷重抓。

# LINK_REFRESH_SOURCES = {'KugouMusicClient', 'KuwoMusicClient', 'NeteaseMusicClient', 'QqMusicClient', 'QQMusicClient', 'MiguMusicClient'}
# LINK_REFRESH_INTERVAL_DAYS = 7   # 7 天内的链认为还活着, 跳过
# LINK_REFRESH_WORKERS = 2         # 并发 daemon 数(2 worker 并发已足以填满搜狗 IP 配额)
# LINK_REFRESH_BATCH = 500         # 单次启动只刷前 N 首(避免搜狗限流封 IP); 下次启动再刷下一批
# LINK_REFRESH_PER_REQUEST_SLEEP = 3.0   # 每个 _refresh_song_link 完后 sleep, 避开搜狗 IP 限频


# def _persist_refreshed_link(group_key, track_index, fresh_url, fresh_song):
#     """把 _refresh_song_link 拿到的 fresh_url 写回库的指定 track, 顺便更新 _last_refresh_at。
#     写回失败仅打印警告(不影响试听), 下次启动再试。"""
#     try:
#         path = _cache_path(group_key)
#         payload = json.loads(path.read_text(encoding='utf-8'))
#         if track_index >= len(payload.get('tracks') or []):
#             return
#         tr = payload['tracks'][track_index]
#         sd = tr.setdefault('_song', {})
#         old = sd.get('download_url')
#         if old != fresh_url:
#             sd['download_url'] = fresh_url
#         if fresh_song is not None:
#             for k in ('ext', 'file_size', 'file_size_bytes', 'duration', 'duration_s', 'bitrate', 'cover_url', 'lyric'):
#                 v = getattr(fresh_song, k, None)
#                 if v not in (None, '', 'None'):
#                     if k in ('file_size_bytes', 'duration_s', 'bitrate') and not isinstance(v, (int, float)):
#                         continue
#                     sd[k] = v
#         tr['_last_refresh_at'] = time.time()
#         _atomic_write_json(path, payload)
#     except Exception as e:
#         print(f'    [_persist] 写回失败 group={group_key} idx={track_index}: {type(e).__name__}: {e}')


# def _link_refresh_worker(worker_id, queue, counters):
#     """从队列拿 (group_key, track_index, song), 跑 _refresh_song_link, 成功后 _persist_refreshed_link。"""
#     from webui.mountainriverechoes import _refresh_song_link as _rl
#     while True:
#         try:
#             item = queue.get_nowait()
#         except Exception:
#             return
#         group_key, idx, song = item
#         try:
#             fresh = _rl(song)
#         except Exception as e:
#             print(f'[link-refresh] {group_key}[{idx}] {song.song_name} 异常: {type(e).__name__}: {e}')
#             with counters['lock']:
#                 counters['fail'] += 1
#             continue
#         if fresh and isinstance(fresh, str) and fresh != song.download_url:
#             _persist_refreshed_link(group_key, idx, fresh, None)
#             with counters['lock']:
#                 counters['ok'] += 1
#             print(f'[link-refresh] {group_key}[{idx}] {song.song_name} -> 重抓 OK')
#         else:
#             with counters['lock']:
#                 counters['miss'] += 1
#             print(f'[link-refresh] {group_key}[{idx}] {song.song_name} miss (old={song.download_url[:50]})')
#         # 每 50 首报一次进度
#         with counters['lock']:
#             done = counters['ok'] + counters['miss'] + counters['fail']
#             if done % 50 == 0 and done > 0:
#                 print(f'[link-refresh] w{worker_id} 进度 ok={counters["ok"]} miss={counters["miss"]} fail={counters["fail"]}')
#         # 避开搜狗 IP 限频: 每个 _refresh_song_link(内部多次搜索) 后 sleep 一下
#         time.sleep(LINK_REFRESH_PER_REQUEST_SLEEP)


# def _start_link_refresh_worker():
#     """服务启动后异步启动后台 worker, 重抓所有 Kugou/Kuwo/Netease/QQ/Migu 源过期链。
#     立即返回(不阻塞启动); 进度写到 /api/tasks。"""
#     counters = {'ok': 0, 'fail': 0, 'miss': 0, 'lock': threading.Lock(), 'started': time.time()}
#     queue = queue_module.Queue()
#
#     # 1) 优先队列: 读 ui_state.json 里的 cm_order_ethrows_* 与 cm_my_playlists, 把歌手顺序第一位的歌先刷
#     priority_paths = set()
#     try:
#         st = _ui_state_load() if False else (json.loads(UI_STATE_PATH.read_text(encoding='utf-8')) if UI_STATE_PATH.exists() else {})
#         for k, v in (st or {}).items():
#             if k.startswith('cm_order_ethrows_') and isinstance(v, dict):
#                 # v 是 {singer_name: index} —— 把这些歌手的歌加进优先队列
#                 pass  # 直接按 ui_state 顶下的歌单库扫一遍
#     except Exception:
#         pass
#
#     # 2) 扫所有 56 族 + han, 收集 LINK_REFRESH_SOURCES 源的过期/缺链歌
#     now = time.time()
#     deadline = now - LINK_REFRESH_INTERVAL_DAYS * 86400
#     items = []
#     for g in ETHNIC_GROUPS:
#         group_key = g['key']
#         path = _cache_path(group_key)
#         if not path.exists():
#             continue
#         try:
#             d = json.loads(path.read_text(encoding='utf-8'))
#         except Exception:
#             continue
#         for i, t in enumerate(d.get('tracks') or []):
#             sd = t.get('_song') or {}
#             # 库里存的 source 带 MusicClient 后缀, LINK_REFRESH_SOURCES 也带 — 直接比对
#             if (sd.get('source') or '') not in LINK_REFRESH_SOURCES:
#                 continue
#             url = sd.get('download_url') or ''
#             if not (isinstance(url, str) and url.startswith('http')):
#                 continue
#             last_at = t.get('_last_refresh_at')
#             if isinstance(last_at, (int, float)) and last_at > deadline:
#                 continue   # 7 天内刚刷过
#             # 构造轻量 SongInfo 供 _refresh_song_link
#             from musicdl.modules import SongInfo as _SI
#             try:
#                 song = _SI.fromdict(sd)
#             except Exception:
#                 continue
#             items.append((group_key, i, song))
#     random.shuffle(items)
#     if len(items) > LINK_REFRESH_BATCH:
#         print(f'[link-refresh] 待刷 {len(items)} 首, 本轮只刷前 {LINK_REFRESH_BATCH} 首(避免搜狗 IP 限流); 下次启动再刷下一批')
#         items = items[:LINK_REFRESH_BATCH]
#     for it in items:
#         queue.put(it)
#     if not items:
#         print(f'[link-refresh] 无过期签名链可刷(Kugou/Kuwo/Netease/QQ/Migu 7 天内已全部最新)')
#         return
#
#     print(f'[link-refresh] 启动后台重抓: 共 {len(items)} 首待刷, {LINK_REFRESH_WORKERS} 个 worker')
#     threads = [threading.Thread(target=_link_refresh_worker, args=(i, queue, counters), daemon=True, name=f'link-refresh-{i}')
#                for i in range(LINK_REFRESH_WORKERS)]
#     t0 = time.time()
#     for t in threads:
#         t.start()
#     for t in threads:
#         t.join(timeout=86400)   # 24h 上限, 兜底防 hang
#     dt = time.time() - t0
#     print(f'[link-refresh] 完成: ok={counters["ok"]} miss={counters["miss"]} fail={counters["fail"]} 用时 {dt:.0f}s')


def _library_song_lookup(name, singers, source):
    """按 (歌名, 歌手, 音源) 三元组在本地民族曲库里回找曲目, 用于 musicdl 内存会话失效后的重定位。

    musicdl 的会话是纯内存态(`search_sessions`, 上限 16 个), **服务重启即全部失效**;
    会话一失效, 库内曲目的 `/api/preview` 就 404, 前端随即走"全网重搜同名曲"兜底 —— 那条路曾把
    《你的微笑》(岳木果) 换成飞儿乐团同名曲。所以这里给一个不依赖会话的回定位入口。

    三个条件必须**完全相等**才认(音源允许带/不带 MusicClient 后缀); 不做任何模糊匹配,
    否则等于又把"同名不同曲"引回来。找不到就返回 None, 让调用方如实 404。
    """
    n, s = str(name or '').strip(), str(singers or '').strip()
    src = str(source or '').strip().removesuffix('MusicClient')
    if not n or not s:
        return None
    loc = _library_identity_index().get((n, s, src))
    if not loc:
        return None
    key, i = loc
    path = ETHNOS_CACHE_DIR / 'han_汉族民间小调.json' if key == 'han' else _cache_path(key)
    from musicdl.modules import SongInfo as _SongInfo
    with suppress(Exception):
        payload = json.loads(path.read_text(encoding='utf-8'))
        sd = ((payload.get('tracks') or [])[i] or {}).get('_song') or {}
        return _SongInfo.fromdict(sd)
    return None


def _persist_song_link(song, orig_src=None):
    """自愈成功后把新直链回写曲库文件: 按 歌名+歌手+音源 定位(定位用自愈前的音源 —— 跨源
    换源会改 song.source), 命中即更新 _song 的直链/音源/identifier。下次播放直接命中,
    不再重复自愈; 定位不到就静默跳过(仅本会话生效)。"""
    with suppress(Exception):
        n, s = str(getattr(song, 'song_name', '') or '').strip(), str(getattr(song, 'singers', '') or '').strip()
        src = str(orig_src or getattr(song, 'source', '') or '').strip().removesuffix('MusicClient')
        if not n or not s or not isinstance(song.download_url, str) or not song.download_url.startswith('http'):
            return
        loc = _library_identity_index().get((n, s, src))
        if not loc:
            return
        key, i = loc
        path = ETHNOS_CACHE_DIR / 'han_汉族民间小调.json' if key == 'han' else _cache_path(key)
        with ethnos_lock:
            payload = json.loads(path.read_text(encoding='utf-8'))
            tr = (payload.get('tracks') or [])[i]
            sd = tr.setdefault('_song', {})
            sd['download_url'] = song.download_url
            new_src = str(getattr(song, 'source', '') or '').removesuffix('MusicClient')
            if new_src:
                sd['source'] = song.source if str(song.source).endswith('MusicClient') else f'{new_src}MusicClient'
                tr['source'] = new_src
            if getattr(song, 'identifier', None):
                sd['identifier'] = song.identifier
            if getattr(song, 'ext', None):
                sd['ext'] = str(song.ext).lstrip('.').lower()
                tr['ext'] = str(song.ext).lstrip('.').upper()
            _atomic_write_json(path, payload)


def _ytdlp_bin():
    """yt-dlp 二进制解析链(为 exe/dmg 打包预留):
    项目 venv(posix bin / win Scripts) -> 打包产物同目录(贴在 exe/dmg 旁) -> miniconda(本机历史路径) -> PATH"""
    cands = []
    for d in (os.path.join(BASE_DIR, 'venv', 'bin'), os.path.join(BASE_DIR, 'venv', 'Scripts'),
              os.path.dirname(sys.executable)):
        for name in ('yt-dlp', 'yt-dlp.exe'):
            cands.append(os.path.join(d, name))
    for c in cands:
        if os.path.exists(c):
            return c
    if os.path.exists('/opt/miniconda3/bin/yt-dlp'):
        return '/opt/miniconda3/bin/yt-dlp'
    return shutil.which('yt-dlp') or 'yt-dlp'


def _ytdlp_audio_pipe(watch_url):
    """B站等会话校验严格的源: yt-dlp stdout 流式管道(自管会话, 全程自洽), 不依赖任何落盘直链"""
    ytdlp_bin = _ytdlp_bin()
    proc = subprocess.Popen([ytdlp_bin, '--no-update', '-q', '-f', 'bestaudio', '-o', '-', watch_url],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def pipe_gen():
        try:
            while True:
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            with suppress(Exception):
                proc.kill()
    resp = Response(pipe_gen(), status=200, content_type='audio/mp4')
    resp.headers['Accept-Ranges'] = 'none'
    return resp


@app.route('/api/preview')
def api_preview():
    sid, item_id = request.args.get('sid'), request.args.get('id')
    touch_user_activity()
    song = session_item(sid, item_id)
    if song is None:
        # 会话已失效(服务重启/被上限挤出): 按曲目身份回本地曲库重定位, 而不是让前端去全网重搜。
        song = _library_song_lookup(request.args.get('n'), request.args.get('s'), request.args.get('src'))
    if song is None:
        return jsonify({'error': '会话过期或曲目不存在'}), 404
    headers = {}
    mc = None
    with state_lock:
        session = search_sessions.get(sid)
        mc = session['client'].music_clients.get(song.source) if session else None
    if mc is not None:
        headers.update(getattr(mc, 'default_download_headers', None) or {})
    headers.update(song.default_download_headers or {})
    headers.setdefault('user-agent', UA)
    if rng := request.headers.get('Range'):
        headers['Range'] = rng
    real_url = _resolve_ytdlp_url(song.download_url)
    if not real_url:
        return jsonify({'error': '音频链接提取失败(yt-dlp)'}), 502
    if 'bilibili' in real_url or 'bilivideo' in real_url:
        # B站CDN校验Referer+UA; 且musicdl内部client的残留头(大写UA/Sec-Ch-Ua等)会导致403, 用全新干净头
        rng2 = request.headers.get('Range')
        headers = {'user-agent': UA, 'Referer': 'https://www.bilibili.com/'}
        if rng2:
            headers['Range'] = rng2
    last_err = None
    try:
        upstream = None
        if isinstance(song.download_url, str) and ('res.wx.qq.com' in song.download_url or 'mpvideo.qpic.cn' in song.download_url):
            # 微信音频/视频CDN: 干净头+Referer加固(mpvideo 强制校验 Referer, 缺了必 403)
            headers = {'user-agent': UA, 'Referer': 'https://mp.weixin.qq.com/'}
            rng3 = request.headers.get('Range')
            if rng3:
                headers['Range'] = rng3
            # mpvideo 签名是绑会话的: 必须用同一个 requests.Session 拿过文章页, 再用同一会话去拉 mpvideo。
            # 没有现成会话的话, 就地拿一次(文章页 + mpvideo)都在这一处搞定, 流转发给浏览器。
            if 'mpvideo.qpic.cn' in song.download_url and not getattr(song, '_wx_sess', None):
                wechat = (((getattr(song, 'raw_data', None) or {}).get('wechat')) or {})
                if not isinstance(wechat, dict):
                    wechat = {}
                _ident = str(getattr(song, 'identifier', '') or '')
                if not (wechat.get('url') or wechat.get('voice_id')) and _ident.startswith('qqvid-'):
                    wechat = {'voice_id': _ident, 'url': '', 'article': ''}
                art_url = wechat.get('url') or ''
                if isinstance(art_url, str) and 'mp.weixin.qq.com' in art_url:
                    fresh_url, sess = _weixin_video_self_heal(art_url, wechat.get('voice_id') or '')
                    if fresh_url:
                        song.download_url = fresh_url
                        song._wx_sess = sess   # 留给可能的 range 续拉
        else:
            # 其他源 CDN 防盗链 Referer (Kugou/Kuwo/Migu/QQ/网易云/v.qq.com 等),
            # 域名匹配的按 RETRY_REFERERS 表加; 没匹配就走默认头
            ref = _referer_for(real_url)
            if ref:
                rng6 = request.headers.get('Range')
                headers = {'user-agent': UA, 'Referer': ref}
                if rng6:
                    headers['Range'] = rng6
        _is_bili = isinstance(song.download_url, str) and song.download_url.startswith('ytdlp:') and 'bilibili.com' in song.download_url
        if _is_bili or 'bilivideo' in (real_url or ''):
            # B站CDN会话校验严格: 直链转发在服务进程内易403, 改用 yt-dlp stdout 流式管道(自管会话, 全程自洽)
            watch_url = _bili_watch_url(song)
            if watch_url:
                return _ytdlp_audio_pipe(watch_url)
        if upstream is None:   # mpvideo 已用会话绑定的请求成功拿到 upstream, 不要被这一行覆写成裸 requests
            wx_sess = getattr(song, '_wx_sess', None)
            if isinstance(wx_sess, requests.Session):
                # 用同一个会话转发 Range(签名链路一致, 否则 dis_k 必 403)
                upstream = wx_sess.get(song.download_url, headers=headers, stream=True, timeout=(5, 20))
            else:
                upstream = requests.get(real_url, headers=headers, stream=True, timeout=(5, 20))
    except Exception as err:
        # 酷狗等CDN证书链不完整会抛 SSLError; 此前这里直接 502 返回, 直链失效的
        # 自愈(重搜换源)根本没机会执行 —— 与 403/404/410 一视同仁, 统一走重搜。
        upstream, last_err = None, err
    if upstream is None or upstream.status_code in (403, 404, 410):
        # 直链过期/不可达: 先试网易云按 identifier 重新签发(电台/DJ音频直链仅约40分钟有效,
        # 落盘即过期, 重搜换源找不到 —— 必须走官方 URL API 续签); 再退回重搜同名曲换源。
        if upstream is not None:
            with suppress(Exception):
                upstream.close()
        src_before = str(song.source or '')   # 自愈可能换源改掉 song.source, 曲库定位要用原音源
        fresh_url = _netease_reissue_url(song) or _refresh_song_link(song)
        if fresh_url:
            retry_headers = {'user-agent': UA}
            # 各源 CDN 防盗链: 微信 mpvoice/mpvideo 走 mp.weixin; 其他按 RETRY_REFERERS 表匹配
            ref = _referer_for(fresh_url)
            if not ref and ('res.wx.qq.com' in fresh_url or 'mpvideo.qpic.cn' in fresh_url):
                ref = 'https://mp.weixin.qq.com/'
            if ref:
                retry_headers['Referer'] = ref
            if rng := request.headers.get('Range'):
                retry_headers['Range'] = rng
            try:
                if fresh_url.startswith('ytdlp:'):
                    # B站自愈结果(ytdlp 形态): 实时解析永不过期, 直接走流式管道并固化入库
                    _persist_song_link(song, src_before)
                    return _ytdlp_audio_pipe(fresh_url[6:])
                upstream = requests.get(fresh_url, headers=retry_headers, stream=True, timeout=(5, 20))
            except Exception:
                return jsonify({'error': '试听失败: 直链已过期且刷新失败'}), 502
            if upstream.status_code not in (403, 404, 410):
                _persist_song_link(song, src_before)   # 新直链已验证可用: 回写曲库, 下次免自愈
        else:
            return jsonify({'error': f'试听失败: 直链失效且未搜到可用替代 ({last_err or "链接过期"})'}), 502
    content_type = upstream.headers.get('Content-Type', '')
    if 'audio' not in content_type and 'octet' not in content_type:
        content_type = mimetypes.guess_type(f'x.{(song.ext or "mp3").lstrip(".")}')[0] or 'audio/mpeg'

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    resp = Response(generate(), status=upstream.status_code, content_type=content_type)
    for h in ('Content-Length', 'Content-Range', 'Accept-Ranges'):
        if h in upstream.headers:
            resp.headers[h] = upstream.headers[h]
    return resp


def proxy_image(url):
    if not isinstance(url, str) or not url.lower().startswith(('http://', 'https://')):
        return jsonify({'error': '非法封面地址'}), 400
    try:
        upstream = requests.get(url, headers={'user-agent': UA, 'referer': f"https://{url.split('/')[2]}/"}, stream=True, timeout=(5, 15))
    except Exception as err:
        return jsonify({'error': f'封面获取失败: {err}'}), 502
    content_type = upstream.headers.get('Content-Type', 'image/jpeg')
    if not content_type.startswith('image'):
        content_type = 'image/jpeg'

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=128 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    resp = Response(generate(), status=upstream.status_code, content_type=content_type)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/api/cover')
def api_cover():
    url = request.args.get('url')
    if not url:
        song = session_item(request.args.get('sid'), request.args.get('id'))
        url = song.cover_url if song else None
    if not url:
        return jsonify({'error': '无封面'}), 404
    return proxy_image(url)


@app.route('/api/lyric', methods=['POST'])
def api_lyric():
    import re as _re
    data = request.get_json(force=True)
    touch_user_activity()
    name, artist = (data.get('name') or '').strip(), (data.get('artist') or '').strip()
    has_ts = lambda l: bool(l and _re.search(r'\[\d+:\d+', str(l)))
    song_lyric, lyric = None, None
    song = session_item(data.get('sid'), data.get('id'))
    if song is not None and song.lyric and str(song.lyric).strip().upper() not in {'NULL', 'NONE'}:
        song_lyric = str(song.lyric)
    if has_ts(song_lyric):
        lyric = song_lyric
    elif name:
        artist = artist.split('/')[0].split(',')[0].strip()
        with suppress(Exception):
            # 优先模糊搜索接口(通常返回带时间轴的syncedLyrics), 精确匹配接口常返回纯文本
            _, lyric = LyricSearchClient.search(track_name=name, artist_name=artist, allowed_lyric_apis=('searchbylrclibapis', 'searchbylrclibapig'))
            lyric = str(lyric) if (lyric and str(lyric).upper() not in {'NULL', 'NONE'}) else None
        if not has_ts(lyric):
            lyric = song_lyric or lyric
    elif song_lyric:
        lyric = song_lyric
    return jsonify({'lyric': lyric})


# ---------------------------------------------------------------- 歌单 / 榜单
def fetch_netease_playlist(playlist_id):
    """网易云歌单快速解析: 只取歌曲元信息(不解析下载链接), 秒级返回"""
    session = requests.Session()
    session.headers.update({'user-agent': UA, 'referer': 'https://music.163.com/'})
    resp = session.post('https://music.163.com/api/v6/playlist/detail', data={'id': playlist_id, 'n': 0}, cookies={'os': 'pc'}, timeout=15)
    resp.raise_for_status()
    playlist = resp.json().get('playlist') or {}
    track_ids = [t['id'] for t in (playlist.get('trackIds') or []) if isinstance(t, dict) and t.get('id')]
    songs = []
    for i in range(0, len(track_ids), 200):
        chunk = track_ids[i:i + 200]
        resp2 = session.post('https://music.163.com/api/v3/song/detail', data={'c': json.dumps([{'id': t} for t in chunk])}, timeout=15)
        resp2.raise_for_status()
        for s in resp2.json().get('songs') or []:
            songs.append({
                'id': s.get('id'), 'name': s.get('name') or '未知曲目',
                'artist': ' / '.join(a.get('name', '') for a in (s.get('ar') or []) if a.get('name')) or '未知歌手',
                'album': (s.get('al') or {}).get('name') or '',
                'cover': (s.get('al') or {}).get('picUrl') or '',
                'duration_ms': s.get('dt') or 0,
            })
    return {'id': str(playlist_id), 'name': playlist.get('name') or f'歌单{playlist_id}',
            'cover': playlist.get('coverImgUrl') or '', 'count': len(songs), 'tracks': songs, 'platform': 'netease', 'platform_name': '网易云音乐'}


def fetch_qq_playlist(playlist_id):
    """QQ音乐歌单快速解析 (musicdl 同源接口, 仅取元数据)"""
    headers = {'User-Agent': UA, 'Referer': f'https://y.qq.com/n/ryqq/playlist/{playlist_id}'}
    resp = requests.get('https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg',
                        params={'disstid': str(playlist_id), 'type': '1', 'json': '1', 'utf8': '1', 'onlysong': '0', 'format': 'json'},
                        headers=headers, timeout=15)
    resp.raise_for_status()
    cdlist = (resp.json().get('cdlist') or [{}])[0]
    songs = []
    for s in cdlist.get('songlist') or []:
        songs.append({
            'id': s.get('songid'), 'name': s.get('songname') or '未知曲目',
            'artist': ' / '.join(a.get('name', '') for a in (s.get('singer') or []) if a.get('name')) or '未知歌手',
            'album': s.get('albumname') or '',
            'cover': f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{s.get('albummid')}.jpg" if s.get('albummid') else '',
            'duration_ms': (s.get('interval') or 0) * 1000,
        })
    return {'id': str(playlist_id), 'name': cdlist.get('dissname') or f'歌单{playlist_id}',
            'cover': cdlist.get('logo') or '', 'count': len(songs), 'tracks': songs, 'platform': 'qq', 'platform_name': 'QQ音乐'}


def fetch_qq_toplist(topid):
    """QQ音乐巅峰榜 (榜单接口, 元数据秒级)"""
    resp = requests.get('https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg',
                        params={'topid': str(topid), 'format': 'json'},
                        headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/'}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    songs = []
    for item in data.get('songlist') or []:
        s = item.get('data') or item
        songs.append({
            'id': s.get('songid'), 'name': s.get('songname') or '未知曲目',
            'artist': ' / '.join(a.get('name', '') for a in (s.get('singer') or []) if a.get('name')) or '未知歌手',
            'album': s.get('albumname') or '',
            'cover': f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{s.get('albummid')}.jpg" if s.get('albummid') else '',
            'duration_ms': (s.get('interval') or 0) * 1000,
        })
    return {'id': str(topid), 'name': data.get('top_title') or f'榜单{topid}',
            'cover': '', 'count': len(songs), 'tracks': songs, 'platform': 'qq', 'platform_name': 'QQ音乐', 'update': data.get('update_time', '')}


def _soda_cover(url_cover):
    with suppress(Exception):
        return str(url_cover['urls'][0]) + str(url_cover['uri']) + '~c5_500x500.jpg'
    return ''


def fetch_soda_playlist(playlist_id):
    """汽水音乐歌单快速解析 (luna 接口, 仅取元数据)"""
    headers = {'User-Agent': UA, 'Referer': 'https://music.douyin.com/'}
    tracks_raw, page, first = [], 1, {}
    while True:
        params = {'playlist_id': str(playlist_id), 'cursor': str(20 * (page - 1)), 'cnt': '20',
                  'aid': '386088', 'device_platform': 'web', 'channel': 'pc_web'}
        try:
            resp = requests.get('https://api.qishui.com/luna/pc/playlist/detail', params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            result = resp.json()
        except Exception:
            break
        media = result.get('media_resources') or []
        if not media:
            break
        tracks_raw.extend(media)
        first = first or result
        with suppress(Exception):
            if float((result.get('playlist') or {}).get('count_tracks', 0)) <= len(tracks_raw):
                break
        page += 1
        if page > 30:
            break
    songs, seen = [], set()
    for mr in tracks_raw:
        track = ((mr.get('entity') or {}).get('track_wrapper') or {}).get('track') or (mr.get('entity') or {}).get('track') or {}
        if not track.get('name') or track.get('id') in seen:
            continue
        seen.add(track.get('id'))
        album = track.get('album') if isinstance(track.get('album'), dict) else {}
        songs.append({
            'id': track.get('id'), 'name': track.get('name'),
            'artist': ' / '.join(a.get('name', '') for a in (track.get('artists') or []) if a.get('name')) or '未知歌手',
            'album': album.get('name') or '',
            'cover': _soda_cover(album.get('url_cover')),
            'duration_ms': track.get('duration') or 0,
        })
    pl = (first.get('playlist') or {})
    return {'id': str(playlist_id), 'name': pl.get('title') or f'歌单{playlist_id}',
            'cover': _soda_cover(pl.get('url_cover')), 'count': len(songs), 'tracks': songs, 'platform': 'soda', 'platform_name': '汽水音乐'}


def resolve_soda_shortlink(url):
    """汽水分享短链 -> playlist_id"""
    from urllib.parse import urlparse, parse_qs
    with suppress(Exception):
        final = requests.head(url, headers={'User-Agent': UA}, allow_redirects=True, timeout=15).url
        if 'playlist_id' in final:
            qs = parse_qs(urlparse(final).query)
            if qs.get('playlist_id'):
                return qs['playlist_id'][0]
    with suppress(Exception):
        qs = parse_qs(urlparse(url).query)
        if qs.get('playlist_id'):
            return qs['playlist_id'][0]
    with suppress(Exception):
        tail = [p for p in urlparse(url).path.split('/') if p]
        if tail:
            return tail[-1].removesuffix('.html')
    return None


def fast_playlist_fetch(platform, playlist_id):
    playlist_id = str(playlist_id)
    if platform == 'netease':
        return fetch_netease_playlist(playlist_id)
    if platform == 'qq_toplist':
        return fetch_qq_toplist(playlist_id)
    if platform == 'qq':
        return fetch_qq_playlist(playlist_id)
    if platform == 'soda':
        return fetch_soda_playlist(playlist_id)
    return None


@app.route('/api/charts')
def api_charts():
    return jsonify({'charts': NETEASE_CHARTS})


@app.route('/api/presetplaylists')
def api_preset_playlists():
    """预置歌单 + 实时元数据(封面/曲数): 已缓存的立即返回, 缺的后台抓取, 前端延时重拉可见"""
    need_refresh = False
    out = []
    for grp in PRESET_PLAYLISTS:
        items = []
        for it in grp['items']:
            key = _preset_meta_key(grp['platform'], it)
            meta = PRESET_META.get(key)
            if meta and time.time() - meta['at'] < PRESET_META_TTL:
                items.append({**it, 'cover': meta['cover'], 'count': meta['count']})
            else:
                need_refresh = True
                items.append(dict(it))
        out.append({**grp, 'items': items})
    if need_refresh:
        threading.Thread(target=_refresh_preset_metas, daemon=True).start()
    return jsonify({'presets': out})


PRESET_META = {}
PRESET_META_TTL = 1800
_preset_meta_lock = threading.Lock()
_preset_meta_refreshing = False


def _preset_meta_key(platform, item):
    pk = 'qq_toplist' if (platform == 'qq' and item.get('kind') == 'toplist') else platform
    return f'{pk}:{item["id"]}'


def _refresh_preset_metas():
    """后台逐个抓取预置歌单元数据(30分钟缓存), 供发现页卡片显示真实封面与曲数"""
    global _preset_meta_refreshing
    with _preset_meta_lock:
        if _preset_meta_refreshing:
            return
        _preset_meta_refreshing = True
    try:
        for grp in PRESET_PLAYLISTS:
            for it in grp['items']:
                key = _preset_meta_key(grp['platform'], it)
                hit = PRESET_META.get(key)
                if hit and time.time() - hit['at'] < PRESET_META_TTL:
                    continue
                try:
                    detail = fast_playlist_fetch('qq_toplist' if (grp['platform'] == 'qq' and it.get('kind') == 'toplist') else grp['platform'], str(it['id']))
                    _t0 = (detail.get('tracks') or [{}])[0]
                    cover = detail.get('cover') or _t0.get('cover_url') or _t0.get('cover') or ''
                    meta = {'cover': cover, 'count': (detail.get('count') or 0) or None, 'at': time.time()}
                except Exception:
                    meta = {'cover': '', 'count': None, 'at': time.time() - PRESET_META_TTL + 300}   # 失败 5 分钟后才重试
                with _preset_meta_lock:
                    PRESET_META[key] = meta
    finally:
        with _preset_meta_lock:
            _preset_meta_refreshing = False


@app.route('/api/playlist', methods=['POST'])
def api_playlist():
    data = request.get_json(force=True)
    target = (data.get('url') or data.get('id') or '').strip()
    platform = (data.get('platform') or '').strip()
    slow = bool(data.get('slow'))
    if platform != 'ethnos':
        touch_user_activity()
    if not target:
        return jsonify({'error': '请输入歌单链接或ID'}), 400
    # 全局索引虚拟歌单: id 形如 artist:歌手名 / album:专辑名@歌手名 / ytcoll:合集名(景颇YouTube合集)
    if platform == 'index':
        kind, _, key = str(target).partition(':')
        if kind not in {'artist', 'album', 'ytcoll'} or not key:
            return jsonify({'error': '非法索引请求'}), 400
        detail = _index_virtual_playlist(kind, key)
        if detail is None:
            return jsonify({'error': '索引中暂无该歌手/专辑的歌曲，请先构建对应民族歌单'}), 404
        return jsonify(detail)
    # 民族音乐歌单: 读磁盘缓存(未构建则触发后台构建); key=han 时读汉族民间小调
    if platform == 'ethnos':
        group = 'han' if str(target) in ('han', '汉族民间小调') else str(target)
        detail = load_ethnos_playlist(group)
        if detail is None:
            with ethnos_lock:
                already = ethnos_state[group]['state'] == 'building' if group in ethnos_state else False
            if not already:
                threading.Thread(target=ethnos_build_worker, args=(group,), daemon=True).start()
            return jsonify({'building': True, 'eta': 40}), 202
        return jsonify(detail)
    # 平台已明确指定 (预置歌单/前端路由)
    if platform and platform in {'netease', 'qq', 'qq_toplist', 'soda'}:
        cache_key = f'{platform}:{target}'
        refresh = bool(data.get('refresh'))   # 前端要求实时: 跳过缓存读(仍写缓存)
        with state_lock:
            cached = playlist_cache.get(cache_key)
        if cached and not refresh and time.time() - cached['at'] < 3600:
            return jsonify(cached['data'])
        try:
            detail = fast_playlist_fetch(platform, target)
        except Exception as err:
            return jsonify({'error': f'歌单解析失败: {str(err)[:120]}'}), 400
        if not detail or not detail.get('tracks'):
            return jsonify({'error': '歌单解析失败或歌单为空'}), 400
        with state_lock:
            playlist_cache[cache_key] = {'data': detail, 'at': time.time()}
        return jsonify(detail)
    # 链接自动识别平台
    if target.lower().startswith('http'):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(target)
        host = (u.netloc or '').lower()
        # 汽水: 短链 or 分享页
        if 'qishui' in host or 'douyin.com' in host:
            playlist_id = resolve_soda_shortlink(target)
            if playlist_id:
                return _respond_fast('soda', playlist_id)
            return jsonify({'error': '无法从汽水链接中解析歌单ID'}), 400
        # QQ音乐
        if 'y.qq.com' in host:
            candidates = []
            with suppress(Exception):
                candidates += parse_qs(u.query).get('id', []) + parse_qs(u.query).get('dissid', [])
            with suppress(Exception):
                tail = [p for p in u.path.split('/') if p]
                if tail:
                    candidates.append(tail[-1])
            playlist_id = next((c for c in candidates if str(c).isdigit()), None)
            if playlist_id:
                return _respond_fast('qq', playlist_id)
            return jsonify({'error': '无法从QQ音乐链接中解析歌单ID'}), 400
        # 网易云
        if '163.com' in host:
            candidates = []
            with suppress(Exception):
                frag = u.fragment
                if '?' in frag:
                    candidates += parse_qs(frag.split('?', 1)[1]).get('id', [])
            with suppress(Exception):
                candidates += parse_qs(u.query).get('id', [])
            with suppress(Exception):
                tail = [p for p in u.path.split('/') if p]
                if tail:
                    candidates.append(tail[-1].removesuffix('.html').removesuffix('.htm'))
            playlist_id = next((c for c in candidates if str(c).isdigit()), None)
            if playlist_id:
                return _respond_fast('netease', playlist_id)
            return jsonify({'error': '无法从链接中解析歌单ID'}), 400
        # 酷狗: 老格式 special/single 全量解析(自动翻页); 新版 gcid 被反爬拦截, 引导文本导入
        if 'kugou.com' in host:
            if 'gcid_' in (u.path or ''):
                return jsonify({'error': '酷狗新版gcid歌单有反爬验证: 服务器无法直接读取, 且网页端本身只展示前10首。请在浏览器打开歌单, 全选复制歌曲列表文本, 用下方"粘贴文本导入"'}), 400
            try:
                client = get_client(['KugouMusicClient'])
                song_infos = client.parseplaylist(target) or []
            except Exception as err:
                return jsonify({'error': f'酷狗歌单解析失败: {str(err)[:120]}'}), 400
            if not song_infos:
                return jsonify({'error': '酷狗歌单解析失败或歌单为空(仅支持 special/single/ 老格式链接)'}), 400
            sid = new_session(client)
            items = search_sessions[sid]['items']
            for i, s in enumerate(song_infos):
                items[f'Playlist#{i}'] = s
            tracks = [dict(song_brief(s, 'Playlist', i), sid=sid) for i, s in enumerate(song_infos)]
            detail = {'id': target, 'name': f'酷狗歌单({len(tracks)}首)', 'cover': (song_infos[0].cover_url if song_infos else ''),
                      'count': len(tracks), 'tracks': tracks, 'sid': sid, 'platform': 'musicdl', 'platform_name': '酷狗歌单'}
            return jsonify(detail)
        # 其他平台 -> 走 musicdl 通用解析(慢)
        slow = True
    elif str(target).isdigit():
        return _respond_fast('netease', target)
# 慢速路径: musicdl 逐首解析 (得到可直接播放/下载的 SongInfo)
    url = target if target.lower().startswith('http') else f'https://music.163.com/#/playlist?id={target}'
    sources = DEFAULT_SOURCES + [s for _, s in HOST_HINTS if s not in DEFAULT_SOURCES]
    try:
        client = get_client(list(dict.fromkeys(sources)))
        song_infos = client.parseplaylist(url) or []
    except Exception as err:
        return jsonify({'error': f'歌单解析失败: {str(err)[:120]}'}), 400
    if not song_infos:
        return jsonify({'error': '歌单解析失败或歌单为空'}), 400
    sid = new_session(client)
    items = search_sessions[sid]['items']
    for i, s in enumerate(song_infos):
        items[f'Playlist#{i}'] = s
    tracks = [dict(song_brief(s, 'Playlist', i), sid=sid) for i, s in enumerate(song_infos)]
    detail = {'id': url, 'name': f'导入歌单({len(tracks)}首)', 'cover': (song_infos[0].cover_url if song_infos else ''), 'count': len(tracks), 'tracks': tracks, 'sid': sid, 'platform': 'musicdl', 'platform_name': '导入歌单'}
    return jsonify(detail)
TEXT_IMPORT_SOURCES = ['KugouMusicClient', 'KuwoMusicClient', 'MiguMusicClient']


def _text_import_norm(s):
    return re.sub(r'[\s\-—·,，.。\'"()（）【】\[\]~～!！?？:：;；&+、]', '', str(s or '').lower())


@app.route('/api/playlist/text', methods=['POST'])
def api_playlist_text():
    """粘贴文本导入: 浏览器里能看到的任何歌单(如酷狗gcid网页只渲染前10首)复制文本后按行搜索匹配导入"""
    data = request.get_json(force=True)
    text = str(data.get('text') or '')
    pl_name = str(data.get('name') or '').strip()
    entries, seen = [], set()
    for line in text.splitlines():
        line = line.strip().lstrip('\u200b')
        if not line:
            continue
        m = re.match(r'^(\d{1,3})[\s.、)）]+(.+)$', line)
        if m:
            line = m.group(2).strip()
        if ' - ' in line:
            singer, name = line.split(' - ', 1)
            singer, name = singer.strip(), name.strip()
        else:
            singer, name = '', line
        if not name:
            continue
        k = _text_import_norm(name) + '|' + _text_import_norm(singer)
        if k in seen or len(entries) >= 300:
            continue
        seen.add(k)
        entries.append((singer, name))
    if not entries:
        return jsonify({'error': '未识别到歌曲行, 格式应为每行"序号 歌手 - 歌名"或"歌手 - 歌名"'}), 400
    client = get_client(TEXT_IMPORT_SOURCES)
    results, unmatched, lock = [], [], threading.Lock()

    def match_one(entry):
        singer, name = entry
        best, best_sc = None, -1
        for kw in ([f'{singer} {name}', name] if singer else [name]):
            for src in TEXT_IMPORT_SOURCES:
                try:
                    songs = client.music_clients[src].search(keyword=kw, num_threadings=2, request_overrides={}, rule={}) or []
                except Exception:
                    songs = []
                for cand in songs:
                    if not cand.song_name:
                        continue
                    cn, wn = _text_import_norm(cand.song_name), _text_import_norm(name)
                    if not cn or not wn or not (cn == wn or wn in cn or cn in wn):
                        continue
                    sc = (100 if cn == wn else 60)
                    if singer and _text_import_norm(singer) and _text_import_norm(singer) in _text_import_norm(cand.singers or ''):
                        sc += 30
                    if cand.protocol == 'HTTP' and isinstance(cand.download_url, str) and cand.download_url.startswith('http'):
                        sc += 20
                    if str(cand.ext or '').upper() in {'FLAC', 'WAV', 'APE'}:
                        sc += 5
                    src_name = (cand.source or '').removesuffix('MusicClient')
                    if src_name in {'Kuwo', 'Migu'}:   # 直链稳定可试听; 酷狗CDN链接会话绑定易失效, 同分避开
                        sc += 15
                    elif src_name == 'Kugou':
                        sc -= 10
                    if sc > best_sc:
                        best, best_sc = cand, sc
                if best_sc >= 100:
                    break
                if best_sc >= 100:
                    break
        if best is not None and (best.source or '') == 'KugouMusicClient':
            # 酷狗CDN直链会话绑定易过期: 补搜酷我/咪咕一次, 同级匹配即换成稳定直链源
            for src in ('KuwoMusicClient', 'MiguMusicClient'):
                try:
                    songs = client.music_clients[src].search(keyword=(f'{singer} {name}' if singer else name), num_threadings=2, request_overrides={}, rule={}) or []
                except Exception:
                    songs = []
                alt, alt_sc = None, -1
                for cand in songs:
                    if not cand.song_name:
                        continue
                    cn, wn = _text_import_norm(cand.song_name), _text_import_norm(name)
                    if not cn or not wn or not (cn == wn or wn in cn or cn in wn):
                        continue
                    sc = (100 if cn == wn else 60)
                    if singer and _text_import_norm(singer) and _text_import_norm(singer) in _text_import_norm(cand.singers or ''):
                        sc += 30
                    if cand.protocol == 'HTTP' and isinstance(cand.download_url, str) and cand.download_url.startswith('http'):
                        sc += 20
                    if sc > alt_sc:
                        alt, alt_sc = cand, sc
                if alt is not None and alt_sc >= best_sc - 25:   # 酷狗基线已-10, 同级容差内即替换
                    best = alt
                    break
        return entry, best if best is not None and best_sc >= 60 else None
    with ThreadPoolExecutor(max_workers=6) as ex:
        for entry, best in ex.map(match_one, entries):
            with lock:
                if best is not None:
                    results.append(best)
                else:
                    unmatched.append(f'{entry[0]} - {entry[1]}' if entry[0] else entry[1])
    if not results:
        return jsonify({'error': f'全部 {len(entries)} 行均未匹配到音源, 请检查文本格式'}), 400
    sid = new_session(client)
    items = search_sessions[sid]['items']
    for i, s in enumerate(results):
        items[f'Playlist#{i}'] = s
    tracks = [dict(song_brief(s, 'Playlist', i), sid=sid) for i, s in enumerate(results)]
    detail = {'id': f'text:{sid}', 'name': pl_name or f'文本导入({len(tracks)}首)', 'cover': (results[0].cover_url if results else ''),
              'count': len(tracks), 'tracks': tracks, 'sid': sid, 'platform': 'musicdl', 'platform_name': '文本导入',
              'unmatched': unmatched, 'total_lines': len(entries)}
    return jsonify(detail)


def _respond_fast(platform, playlist_id):
    cache_key = f'{platform}:{playlist_id}'
    with state_lock:
        cached = playlist_cache.get(cache_key)
    if cached and time.time() - cached['at'] < 3600:
        return jsonify(cached['data'])
    try:
        detail = fast_playlist_fetch(platform, playlist_id)
    except Exception as err:
        return jsonify({'error': f'歌单解析失败: {str(err)[:120]}'}), 400
    if not detail or not detail.get('tracks'):
        return jsonify({'error': '歌单解析失败或歌单为空'}), 400
    with state_lock:
        playlist_cache[cache_key] = {'data': detail, 'at': time.time()}
    return jsonify(detail)


@app.route('/api/ethnos/ingest', methods=['POST'])
def api_ethnos_ingest():
    """外部数据统一入库通道: 服务端内读盘-合并-写盘(带锁), 避免与后台构建的写盘竞争"""
    data = request.get_json(force=True)
    group_key = data.get('group')
    if group_key not in ETHNIC_BY_KEY:
        return jsonify({'error': '未知的民族'}), 400
    info = ETHNIC_BY_KEY[group_key]
    tracks = data.get('tracks') or []
    if not tracks:
        return jsonify({'error': '无曲目'}), 400
    path = _cache_path(group_key)
    with ethnos_lock:
        payload = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'v': ETHNOS_SCHEMA, 'tracks': []}
        existing = {re.sub(r'[\W_]+', '', str(t.get('song_name', '')).lower())[:40] for t in payload.get('tracks') or []}
        added = 0
        for t in tracks:
            k = re.sub(r'[\W_]+', '', str(t.get('song_name', '')).lower())[:40]
            if not t.get('song_name') or not k or k in existing:
                continue
            existing.add(k)
            payload.setdefault('tracks', []).append(t)
            added += 1
        payload['count'] = len(payload.get('tracks') or [])
        if payload.get('v') != ETHNOS_SCHEMA:
            payload['v'] = ETHNOS_SCHEMA
        _atomic_write_json(path, payload)
        if ethnos_state[group_key]['state'] == 'done':
            ethnos_state[group_key] = {'state': 'done', 'count': payload['count']}
    return jsonify({'ok': True, 'added': added, 'total': payload['count']})


def build_han_folk():
    """汉族民间小调搜索构建: 各省小调/民歌/民乐/戏曲 + 权威民歌手"""
    path = ETHNOS_CACHE_DIR / 'han_汉族民间小调.json'
    with ethnos_lock:
        if HAN_BUILD_STATE['state'] == 'building':
            return False
        HAN_BUILD_STATE.update(state='building', progress='准备中', count=0)
    try:
        client = _ethnos_get_client()
        merged, warm_keys = {}, set()
        with suppress(Exception):
            if path.exists():
                _old = json.loads(path.read_text(encoding='utf-8'))
                if _old.get('v') == ETHNOS_SCHEMA:
                    from musicdl.modules import SongInfo as _SI
                    for _t in _old.get('tracks') or []:
                        with suppress(Exception):
                            _s = _SI.fromdict(_t.get('_song') or {})
                            if _s.song_name:
                                _k = _dedup_key(_s)
                                merged[_k] = (_s, 1000)
                                warm_keys.add(_k)
        MINOR = ('藏族', '蒙古', '维吾尔', '彝族', '苗族', '侗族', '瑶族', '壮族', '傣族', '佤族', '景颇', '朝鲜', '哈萨克', '回族', '东乡', '保安', '撒拉', '土族', '裕固', '门巴', '珞巴', '基诺', '怒族', '德昂', '布朗', '阿昌', '普米', '塔吉克', '乌孜别克', '俄罗斯', '鄂温克', '鄂伦春', '赫哲', '达斡尔', '仫佬', '毛南', '仡佬', '锡伯', '水族', '拉祜', '畲族', '高山', '布依', '白族', '羌族', '黎族', '傈僳', '独龙', '京族', '哈尼', '土家', '纳西', '柯尔克孜', '塔塔尔')
        def is_han(t_name, t_album):
            blob = f"{t_name}{t_album}"
            return not any(m in blob for m in MINOR)
        kws = HAN_FOLK_KWS + HAN_EXTRA_ARTISTS
        for kw_idx, kw in enumerate(kws):
            if ethnos_stop.is_set():
                break
            for song in _search_ethnos_keyword(client, kw):
                if not song.song_name or not is_han(str(song.song_name), str(song.album or '')):
                    continue
                key = _dedup_key(song)
                if not key.strip('|') or key in warm_keys:
                    continue
                score = (900 + (100 if song.protocol == 'HTTP' and isinstance(song.download_url, str) and song.download_url.startswith('http') else 0)
                         + (10 if str(song.ext or '').upper() in {'FLAC', 'WAV', 'APE'} else 0))
                prev = merged.get(key)
                if prev is None or score > prev[1]:
                    merged[key] = (song, score)
            with ethnos_lock:
                HAN_BUILD_STATE['count'] = len(merged)
                HAN_BUILD_STATE['progress'] = f'{kw_idx+1}/{len(kws)}: {kw}'
            songs_sorted = [v[0] for v in sorted(merged.values(), key=lambda x: -x[1])]
            _atomic_write_json(path, _han_payload(songs_sorted, partial=True))
            time.sleep(2)
        songs = [v[0] for v in sorted(merged.values(), key=lambda x: -x[1])]
        _atomic_write_json(path, _han_payload(songs, partial=False))
        with ethnos_lock:
            HAN_BUILD_STATE.update(state='done', progress='', count=len(songs))
        return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        with ethnos_lock:
            HAN_BUILD_STATE.update(state='idle', progress=str(e)[:80], count=0)
        return False


def _han_payload(songs, partial=False):
    tracks_payload = []
    for i, s in enumerate(songs):
        brief = song_brief(s, 'Han', i)
        real = (s.source or '').removesuffix('MusicClient')
        if isinstance(s.download_url, str) and s.download_url.startswith('ytdlp:'):
            real = 'Bilibili' if 'bilibili.com' in s.download_url else 'YouTube'
        elif isinstance(s.download_url, str) and 'res.wx.qq.com' in s.download_url:
            real = '光音·微信'
        brief['source'] = real or 'Han'
        brief['source_cn'] = SOURCE_NAMES.get(real, real) if real else '民间小调'
        tracks_payload.append(dict(brief, _song=_slim_song(s.todict())))
    return {'v': ETHNOS_SCHEMA, 'partial': partial, 'name': '汉族民间小调', 'group': '汉族民间小调',
            'key': 'han', 'cover': (songs[0].cover_url if songs and songs[0].cover_url else ''),
            'count': len(songs), 'built_at': time.time(), 'kws': HAN_FOLK_KWS, 'sources': [], 'tracks': tracks_payload}


HAN_BUILD_STATE = {'state': 'idle', 'progress': '', 'count': 0}


@app.route('/api/ethnos/edit', methods=['POST'])
def api_ethnos_edit():
    """协作编辑: 增删歌手(元数据块)/删歌/加歌, 只触碰目标民族的单个文件"""
    data = request.get_json(force=True)
    group = data.get('group')
    if group not in ETHNIC_BY_KEY and group != 'han':
        return jsonify({'error': '未知的民族'}), 400
    path = _cache_path(group)
    with ethnos_lock:
        if not path.exists():
            return jsonify({'error': '歌单文件不存在'}), 404
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload.setdefault('artists_added', [])
        payload.setdefault('artists_removed', [])
        payload.setdefault('artists_pinned', [])
        payload.setdefault('artist_order', [])
        payload.setdefault('collections_removed', [])   # 被隐藏的合集名(曲目保留, 与歌手同权)
        payload.setdefault('tracks_pinned', [])
        payload.setdefault('track_order', [])
        payload.setdefault('edited_by', [])
        payload.setdefault('edited_at', [])
        editor = (data.get('editor') or 'anonymous')[:24]
        name = (data.get('name') or '').strip()
        extra = {}          # 各 action 想回传给前端的附加统计
        if action := data.get('action'):
            if action == 'add_artist':
                if name and name not in payload['artists_added']:
                    payload['artists_added'].append(name)
                    payload['artists_removed'] = [x for x in payload['artists_removed'] if x != name]
            elif action == 'remove_artist':
                if name and name not in payload['artists_removed']:
                    payload['artists_removed'].append(name)
                    payload['artists_added'] = [x for x in payload['artists_added'] if x != name]
                # kill_tracks: 连同名下曲目一起硬删(任何署名含该歌手的条目), 卡片与曲目同时消失
                if name and bool(data.get('kill_tracks')):
                    def _sung_by(t):
                        parts = [p.strip() for p in str(t.get('singers') or '').split('/') if p.strip()]
                        return name in parts
                    _before = len(payload.get('tracks') or [])
                    payload['tracks'] = [t for t in payload.get('tracks') or [] if not _sung_by(t)]
                    payload['count'] = len(payload['tracks'])
                    extra['killed'] = _before - payload['count']
            elif action == 'save_artist_order':
                # 置顶/拖动: 前端发完整显示顺序 names 与置顶集合 pinned, 后端拆成两组落盘
                # (只存名单不存位置索引, 新增歌手天然排在未排序区, 不会因为索引失效而乱)
                names = [str(n).strip() for n in (data.get('names') or []) if str(n).strip()]
                pinned = [str(n).strip() for n in (data.get('pinned') or []) if str(n).strip()]
                payload['artists_pinned'] = list(dict.fromkeys(pinned))
                payload['artist_order'] = [n for n in dict.fromkeys(names) if n not in payload['artists_pinned']]
                extra['pinned'] = len(payload['artists_pinned'])
            elif action == 'save_track_order':
                # 曲目置顶/拖动: keys 为完整显示顺序(复合键 dict), pinned 为置顶子集
                keys = [k for k in (data.get('keys') or []) if isinstance(k, dict)]
                pinned = {_track_rawkey(k) for k in (data.get('pinned') or []) if isinstance(k, dict)}
                seq = [_track_rawkey(k) for k in keys if isinstance(k, dict)]
                payload['tracks_pinned'] = list(dict.fromkeys([_track_rawkey(k) for k in (data.get('pinned') or []) if isinstance(k, dict)]))
                payload['track_order'] = [k for k in dict.fromkeys(seq) if k not in payload['tracks_pinned']]
                extra['pinned'] = len(payload['tracks_pinned'])
            elif action == 'reassign_singer':
                # 同库内改归属: 把某条曲目的歌手改成目标歌手(修正错误归属/未署名)
                tname = (data.get('tname') or '').strip()
                tsingers = (data.get('tsingers') or '').strip()
                tsource = (data.get('tsource') or '').strip()
                newsinger = (data.get('new_singers') or '').strip()
                if not newsinger:
                    return jsonify({'error': '缺少目标歌手'}), 400
                want = _track_key({'song_name': tname, 'singers': tsingers, 'source': tsource})
                hit = 0
                for t in payload.get('tracks') or []:
                    if _track_key(t) == want:
                        t['singers'] = newsinger
                        (t.get('_song') or {}).update({'singers': newsinger})
                        hit += 1
                        break
                if not hit:
                    return jsonify({'error': '未找到该曲目'}), 404
                # 挂歌到某歌手名下 => 该歌手必须可见: 从移除名单里解禁(否则卡片被 filtered 掉,
                # 表现成"新建成功却搜不到")
                if newsinger in (payload.get('artists_removed') or []):
                    payload['artists_removed'] = [x for x in payload['artists_removed'] if x != newsinger]
                if newsinger not in (payload.get('artists_added') or []):
                    payload.setdefault('artists_added', []).append(newsinger)
                _ui_unban_artist(group, [newsinger])
                extra['reassigned'] = newsinger
            elif action == 'add_to_artist':
                # 加入某歌手名下(目标民族库): 从常驻会话取完整曲目对象落盘, 指纹去重
                dst_key = (data.get('dst') or '').strip()
                if dst_key not in ETHNIC_BY_KEY and dst_key != 'han':
                    return jsonify({'error': '未知的目标民族'}), 400
                artist = (data.get('artist') or '').strip()
                if not artist:
                    return jsonify({'error': '缺少目标歌手'}), 400
                song = session_item(data.get('sid'), data.get('tid'))
                if song is None:
                    return jsonify({'error': '会话已过期, 请重新搜索后再试'}), 404
                if not isinstance(song.download_url, str) or not song.download_url.startswith('http'):
                    return jsonify({'error': '该曲目没有可用直链, 无法入库'}), 400
                dpath = _cache_path(dst_key)
                if not dpath.exists():
                    return jsonify({'error': f'目标歌单文件不存在: {dpath.name}'}), 404
                dp = json.loads(dpath.read_text(encoding='utf-8'))
                dtracks = dp.setdefault('tracks', [])
                fp_key = _track_key({'song_name': song.song_name, 'singers': artist, 'source': (song.source or '')})
                if any(_track_key({'song_name': t.get('song_name'), 'singers': t.get('singers'), 'source': t.get('source')}) == fp_key for t in dtracks):
                    return jsonify({'error': f'《{song.song_name}》已在「{artist}」名下'}), 409
                mx = 0
                for t in dtracks:
                    with suppress(Exception):
                        mx = max(mx, int(str(t.get('id', '0')).split('#')[-1]))
                sd = song.todict()
                sd['singers'] = artist
                brief = {
                    'id': f'Ethnos#{mx + 1}', 'song_name': song.song_name, 'singers': artist,
                    'album': song.album or '', 'ext': (song.ext or '').lstrip('.').upper(),
                    'file_size': song.file_size or '', 'duration': song.duration if ':' in str(song.duration or '') else '',
                    'duration_s': song.duration_s, 'bitrate': song.bitrate, 'cover_url': song.cover_url or '',
                    'lyric': bool(song.lyric), 'source': (song.source or '').removesuffix('MusicClient'),
                    'source_cn': SOURCE_NAMES.get((song.source or '').removesuffix('MusicClient'), (song.source or '').removesuffix('MusicClient')),
                    'previewable': True, 'downloadable': True, 'assigned_by': editor, '_song': sd,
                }
                dtracks.append(brief)
                # 同样解禁: 目标民族若曾移除过该歌手, 现在挂了新歌就应重新可见
                if artist in (dp.get('artists_removed') or []):
                    dp['artists_removed'] = [x for x in dp['artists_removed'] if x != artist]
                if artist not in (dp.get('artists_added') or []):
                    dp.setdefault('artists_added', []).append(artist)
                dp['count'] = len(dtracks)
                dp.setdefault('edited_by', []).append(f'{editor}:add_to_artist:{dst_key}:{artist}')
                dp.setdefault('edited_at', []).append(int(time.time()))
                dp['edited_by'] = dp['edited_by'][-50:]
                dp['edited_at'] = dp['edited_at'][-50:]
                _atomic_write_json(dpath, dp)
                if dst_key in ethnos_state and ethnos_state[dst_key]['state'] == 'done':
                    ethnos_state[dst_key]['count'] = dp['count']
                _ui_unban_artist(dst_key, [artist])
                # 目标文件已落盘, 直接返回(不再走本函数末尾对 group 文件的重复写)
                return jsonify({'ok': True, 'added': 1, 'dst': dst_key, 'dst_count': dp['count'], 'artist': artist})
            elif action == 'remove_track':
                # 按复合键(歌名+歌手+音源)定位, 不再只按歌名 —— 旧逻辑会把同名曲目一次性全删
                # (实测在景颇族删 1 首《目瑙纵歌》连同删除 24 条同名不同歌手的曲目)
                tname = (data.get('tname') or '').strip()
                tsingers = (data.get('tsingers') or '').strip()
                tsource = (data.get('tsource') or '').strip()

                want = _track_key({'song_name': tname, 'singers': tsingers, 'source': tsource})
                before = len(payload.get('tracks') or [])
                if tsingers or tsource:
                    payload['tracks'] = [t for t in payload.get('tracks') or [] if _track_key(t) != want]
                else:
                    # 只有歌名的旧式调用: 不再批量删, 仅删掉第一条匹配(宁可少删不可误删)
                    _idx = next((i for i, t in enumerate(payload.get('tracks') or []) if str(t.get('song_name', '')).strip() == tname), None)
                    if _idx is not None:
                        payload['tracks'].pop(_idx)
                if before == len(payload['tracks']):
                    return jsonify({'error': '未找到该曲目'}), 404
                payload['count'] = len(payload['tracks'])
            elif action == 'remove_tracks':
                # 批量删除: keys=[{song_name,singers,source}...], 一律按复合键逐条精确定位
                keys = [k for k in (data.get('keys') or []) if isinstance(k, dict)]
                if not keys:
                    return jsonify({'error': '缺少曲目列表'}), 400
                want = {_track_key(k) for k in keys}
                before = len(payload.get('tracks') or [])
                payload['tracks'] = [t for t in payload.get('tracks') or [] if _track_key(t) not in want]
                extra['removed'] = before - len(payload['tracks'])
                if not extra['removed']:
                    return jsonify({'error': '未找到匹配的曲目'}), 404
                payload['count'] = len(payload['tracks'])
            elif action == 'move_tracks':
                # 跨文件移动/复制: 从本民族取出若干曲目追加到目标民族歌单, 两个文件各自原子写
                # mode: move=源端删除(默认) / copy=源端保留(两边都有)
                dst_key = (data.get('dst') or '').strip()
                if dst_key not in ETHNIC_BY_KEY and dst_key != 'han':
                    return jsonify({'error': '未知的目标民族'}), 400
                if dst_key == group:
                    return jsonify({'error': '目标民族与来源相同'}), 400
                keys = [k for k in (data.get('keys') or []) if isinstance(k, dict)]
                if not keys:
                    return jsonify({'error': '缺少曲目列表'}), 400
                mode = 'copy' if str(data.get('mode') or 'move').lower() == 'copy' else 'move'
                want = {_track_key(k) for k in keys}
                src_tracks = payload.get('tracks') or []
                picked = [t for t in src_tracks if _track_key(t) in want]
                if not picked:
                    return jsonify({'error': '未找到匹配的曲目'}), 404
                dpath = _cache_path(dst_key)
                if not dpath.exists():
                    return jsonify({'error': f'目标歌单文件不存在: {dpath.name}'}), 404
                dp = json.loads(dpath.read_text(encoding='utf-8'))
                if dp.get('v') != ETHNOS_SCHEMA:
                    return jsonify({'error': '目标歌单版本不符'}), 400
                dtracks = dp.setdefault('tracks', [])
                have = {_track_key(t) for t in dtracks}
                added = [t for t in picked if _track_key(t) not in have]
                if not added:
                    return jsonify({'error': '这些曲目目标歌单里已存在'}), 400
                dtracks.extend(added)
                dp['count'] = len(dtracks)
                dp.setdefault('edited_by', []).append(f'{editor}:move_in:{dst_key}:{len(added)}')
                dp.setdefault('edited_at', []).append(int(time.time()))
                dp['edited_by'] = dp['edited_by'][-50:]
                dp['edited_at'] = dp['edited_at'][-50:]
                _atomic_write_json(dpath, dp)
                if mode == 'move':
                    payload['tracks'] = [t for t in src_tracks if _track_key(t) not in want]
                    payload['count'] = len(payload['tracks'])
                if dst_key in ethnos_state and ethnos_state[dst_key]['state'] == 'done':
                    ethnos_state[dst_key]['count'] = dp['count']
                extra.update(moved=len(added), mode=mode, dst=dst_key, dst_count=dp['count'])
            elif action == 'move_artist':
                # 整个歌手搬库「移动至」: 把该歌手(singers 命中)名下曲目从本民族移到目标民族,
                # 并清理两边的歌手名单。与 move_tracks 同源, 差别只是"按 singers 选曲"而非按复合键。
                dst_key = (data.get('dst') or '').strip()
                if dst_key not in ETHNIC_BY_KEY and dst_key != 'han':
                    return jsonify({'error': '未知的目标民族'}), 400
                if dst_key == group:
                    return jsonify({'error': '目标民族与来源相同'}), 400
                if not name:
                    return jsonify({'error': '缺少歌手名'}), 400

                def _sung_by(t):
                    parts = [p.strip() for p in str(t.get('singers') or '').split('/') if p.strip()]
                    return name in parts
                src_tracks = payload.get('tracks') or []
                picked = [t for t in src_tracks if _sung_by(t)]
                if not picked:
                    return jsonify({'error': f'「{name}」名下没有曲目'}), 404
                dpath = _cache_path(dst_key)
                if not dpath.exists():
                    return jsonify({'error': f'目标歌单文件不存在: {dpath.name}'}), 404
                dp = json.loads(dpath.read_text(encoding='utf-8'))
                if dp.get('v') != ETHNOS_SCHEMA:
                    return jsonify({'error': '目标歌单版本不符'}), 400
                dtracks = dp.setdefault('tracks', [])
                have = {_track_key(t) for t in dtracks}
                added = [t for t in picked if _track_key(t) not in have]
                dtracks.extend(added)
                dp['count'] = len(dtracks)
                # 目标: 该歌手重新可见(从移除名单解禁并入册)
                if name in (dp.get('artists_removed') or []):
                    dp['artists_removed'] = [x for x in dp['artists_removed'] if x != name]
                if name not in (dp.get('artists_added') or []):
                    dp.setdefault('artists_added', []).append(name)
                dp.setdefault('edited_by', []).append(f'{editor}:move_artist_in:{dst_key}:{name}:{len(added)}')
                dp.setdefault('edited_at', []).append(int(time.time()))
                dp['edited_by'] = dp['edited_by'][-50:]
                dp['edited_at'] = dp['edited_at'][-50:]
                _atomic_write_json(dpath, dp)
                if dst_key in ethnos_state and ethnos_state[dst_key]['state'] == 'done':
                    ethnos_state[dst_key]['count'] = dp['count']
                _ui_unban_artist(dst_key, [name])
                # 源: 删曲目 + 清掉该歌手在本民族所有名单里的痕迹(本函数末尾统一写源文件)
                payload['tracks'] = [t for t in src_tracks if not _sung_by(t)]
                payload['count'] = len(payload['tracks'])
                for _k in ('artists_added', 'artists_removed', 'artists_pinned', 'artist_order'):
                    if isinstance(payload.get(_k), list):
                        payload[_k] = [x for x in payload[_k] if x != name]
                extra.update(moved=len(added), mode='move', dst=dst_key, dst_count=dp['count'],
                             src_left=payload['count'], artist=name)
            elif action in ('remove_collection', 'move_collection'):
                """合集(YouTube 合辑等按 album 聚合的虚拟歌单)与歌手完全同权: 可隐藏/连曲目硬删/搬库。
                   选曲口径由「singers 命中」换成「album 完全相等」, 其余写法沿用 move_artist。"""
                if not name:
                    return jsonify({'error': '缺少合集名'}), 400
                payload.setdefault('collections_removed', [])

                def _in_coll(t):
                    return str(t.get('album') or '').strip() == name
                src_tracks = payload.get('tracks') or []
                picked = [t for t in src_tracks if _in_coll(t)]

                if action == 'remove_collection':
                    if bool(data.get('restore')):
                        # 恢复: 只从隐藏名单摘掉, 曲目从未被真删过
                        payload['collections_removed'] = [x for x in payload['collections_removed'] if x != name]
                        extra.update(n=len(picked), mode='restore', restored=not len(picked))
                    elif bool(data.get('kill_tracks')):
                        payload['tracks'] = [t for t in src_tracks if not _in_coll(t)]
                        payload['count'] = len(payload['tracks'])
                        extra.update(killed=len(picked), src_left=payload['count'], mode='kill')
                    else:
                        # 仅隐藏卡片: 曲目留在库里, 随时可恢复(与歌手的"仅隐藏"同语义)
                        if name not in payload['collections_removed']:
                            payload['collections_removed'].append(name)
                        extra.update(n=len(picked), mode='hide')
                else:   # move_collection: 整个合集搬库(曲目的 album 字段跟着走, 到新库自然还是同名合集)
                    if not picked:
                        return jsonify({'error': f'「{name}」内没有曲目'}), 404
                    dst_key = (data.get('dst') or '').strip()
                    if dst_key not in ETHNIC_BY_KEY and dst_key != 'han':
                        return jsonify({'error': '未知的目标民族'}), 400
                    if dst_key == group:
                        return jsonify({'error': '目标民族与来源相同'}), 400
                    dpath = _cache_path(dst_key)
                    if not dpath.exists():
                        return jsonify({'error': f'目标歌单文件不存在: {dpath.name}'}), 404
                    dp = json.loads(dpath.read_text(encoding='utf-8'))
                    if dp.get('v') != ETHNOS_SCHEMA:
                        return jsonify({'error': '目标歌单版本不符'}), 400
                    dtracks = dp.setdefault('tracks', [])
                    have = {_track_key(t) for t in dtracks}
                    added = [t for t in picked if _track_key(t) not in have]
                    dtracks.extend(added)
                    dp['count'] = len(dtracks)
                    dp.setdefault('collections_removed', [])
                    if name in dp['collections_removed']:
                        dp['collections_removed'] = [x for x in dp['collections_removed'] if x != name]   # 搬过去就该看得见
                    dp.setdefault('edited_by', []).append(f'{editor}:move_collection_in:{dst_key}:{name}:{len(added)}')
                    dp.setdefault('edited_at', []).append(int(time.time()))
                    dp['edited_by'] = dp['edited_by'][-50:]
                    dp['edited_at'] = dp['edited_at'][-50:]
                    _atomic_write_json(dpath, dp)
                    if dst_key in ethnos_state and ethnos_state[dst_key]['state'] == 'done':
                        ethnos_state[dst_key]['count'] = dp['count']
                    payload['tracks'] = [t for t in src_tracks if not _in_coll(t)]
                    payload['count'] = len(payload['tracks'])
                    payload['collections_removed'] = [x for x in payload['collections_removed'] if x != name]
                    extra.update(moved=len(added), mode='move', dst=dst_key, dst_count=dp['count'],
                                 src_left=payload['count'], collection=name)
            elif action == 'add_tracks':
                existing = {re.sub(r'[\W_]+', '', str(t.get('song_name', '')).lower())[:40] for t in payload.get('tracks') or []}
                for t in data.get('tracks') or []:
                    k = re.sub(r'[\W_]+', '', str(t.get('song_name', '')).lower())[:40]
                    if t.get('song_name') and k and k not in existing:
                        existing.add(k)
                        payload.setdefault('tracks', []).append(t)
                payload['count'] = len(payload.get('tracks') or [])
        payload['edited_by'].append(f'{editor}:{data.get("action")}:{name or data.get("tname") or ""}')
        payload['edited_at'].append(int(time.time()))
        payload['edited_by'] = payload['edited_by'][-50:]
        payload['edited_at'] = payload['edited_at'][-50:]
        _atomic_write_json(path, payload)
        if group in ethnos_state and ethnos_state[group]['state'] == 'done':
            ethnos_state[group]['count'] = payload['count']
    return jsonify({'ok': True, 'count': payload['count'], **extra})


@app.route('/api/ethnos/custom')
def api_ethnos_custom():
    """读取某民族的协作歌手增删名单"""
    group = request.args.get('group')
    if group not in ETHNIC_BY_KEY and group != 'han':
        return jsonify({'error': '未知的民族'}), 400
    path = _cache_path(group)
    with suppress(Exception):
        payload = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        return jsonify({'artists_added': payload.get('artists_added') or [], 'artists_removed': payload.get('artists_removed') or [],
                        'artists_pinned': payload.get('artists_pinned') or [], 'artist_order': payload.get('artist_order') or [],
                        'collections_removed': payload.get('collections_removed') or [],
                        'tracks_pinned': payload.get('tracks_pinned') or [], 'track_order': payload.get('track_order') or []})
    return jsonify({'artists_added': [], 'artists_removed': [], 'artists_pinned': [], 'artist_order': [], 'collections_removed': [], 'tracks_pinned': [], 'track_order': []})


# ---------------------------------------------------------------- UI 态持久化
# 卡片/歌手拖动次序、我的歌单、隐藏曲目等原先只存浏览器 localStorage —— 清缓存/换设备即丢,
# 且换浏览器后同一份曲库显示两套样子。这里提供服务端单一事实来源, 前端仍用 localStorage 做首屏即时渲染。
def _ui_state_load():
    """读原始结构: {key: {'v': 值, 't': 写入时间戳}}
    每项带时间戳是为了多端(多浏览器/清缓存后重装)取舍: 谁的写入更新听谁的,
    否则本地那份旧值会把用户在别处刚做的排序碾回去。"""
    raw = {}
    with UI_STATE_LOCK:
        with suppress(Exception):
            if UI_STATE_PATH.exists():
                v = json.loads(UI_STATE_PATH.read_text(encoding='utf-8'))
                if isinstance(v, dict):
                    raw = v
    now = int(time.time())
    return {k: (e if isinstance(e, dict) and 'v' in e else {'v': e, 't': now})
            for k, e in raw.items()}


def _ui_unban_artist(group_key, names):
    """把歌手从服务端 UI 态的"已移除"名单里解禁。
    库文件的 artists_removed 只管索引聚合, 前端的歌手卡片过滤读的是 ui_state.json
    的 cm_ethnic_custom —— 两处都得清, 少一处就表现为"挂了歌还是搜不到"。"""
    if not names:
        return
    with suppress(Exception):
        st = _ui_state_load()
        e = st.get('cm_ethnic_custom')
        if not e or not isinstance(e.get('v'), dict):
            return
        gname = None
        for g in ETHNIC_GROUPS:
            if g['key'] == group_key:
                gname = g['name']
                break
        if gname is None and group_key == 'han':
            gname = '汉族民间小调'
        if not gname:
            return
        cc = e['v'].get(gname)
        if not isinstance(cc, dict):
            return
        before = list(cc.get('removed') or [])
        cc['removed'] = [x for x in before if x not in set(names)]
        if cc['removed'] != before:
            added = list(cc.get('added') or [])
            for n in names:
                if n not in added:
                    added.append(n)
            cc['added'] = added
            e['t'] = int(time.time())
            _ui_state_save(st)

UI_TS_TOLERANCE_MS = 86400_000      # 允许的时间戳「未来」余量: 1 天(容忍设备间轻微时钟漂移)


def _sane_ui_ts(t, now_ms):
    """UI 态时间戳合理性闸门(纯函数, 单测见 tools_ui_ts_test.py)。

    只做一件事: 把「超出 now+1 天」的时间戳一律替换为 now。不退化成「越新越好」之外的任何换算
    (秒/毫秒原样保留), 因为这里要挡的不是单位问题, 而是**某个键被一个未来时间戳永久锁死**:
    服务端「新者胜」(`t < cur[key].t` 就跳过) 与前端「谁的时间戳新听谁的」都会永远判服务端那份更新,
    于是该键既写不进去、又每次刷新覆盖本机 —— 2026-10-02「置顶刷新就丢、还弹恢复提示」就是这么来的。
    """
    try:
        t = int(t)
    except (TypeError, ValueError):
        return now_ms
    if t <= 0 or t > now_ms + UI_TS_TOLERANCE_MS:
        return now_ms
    return t


def _ui_ts_floor(t, now_ms):
    """读取既存时间戳时的合理性处理(纯函数, 单测见 tools_ui_ts_test.py)。

    与 _sane_ui_ts 配对使用: 写入时挡住新的异常值, 读取既存值时把已被污染的异常值降级为 0,
    使新写入必然通过 `t < prev` 的「新者胜」判定 —— 系统因此能自愈, 不依赖人工改文件。
    """
    try:
        t = int(t)
    except (TypeError, ValueError):
        return 0
    if t < 0 or t > now_ms + UI_TS_TOLERANCE_MS:
        return 0
    return t


def _ui_state_save(data):
    with UI_STATE_LOCK:
        tmp = UI_STATE_PATH.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(UI_STATE_PATH)


@app.route('/api/ui/state')
def api_ui_state():
    """读取全部持久化 UI 态。
    返回 {v: {key: 值}, t: {key: 时间戳}} —— 值与时间戳分开给, 前端据此做新旧取舍"""
    st = _ui_state_load()
    return jsonify({'v': {k: e['v'] for k, e in st.items()},
                    't': {k: e.get('t', 0) for k, e in st.items()}})



def _sanitize_eth_custom(v):
    """cm_ethnic_custom 入库前清洗: removed 名单与各民族库文件的移除名单对齐。
    库里已解禁的歌手(挂过歌/恢复过), 不允许再被浏览器里的旧 removed 名单带回来 ——
    否则会出现"歌挂上了、卡片永远搜不到"的死循环。"""
    with suppress(Exception):
        for gname, cc in (v or {}).items():
            if not isinstance(cc, dict) or not (cc.get('removed') or []):
                continue
            gk = None
            for g in ETHNIC_GROUPS:
                if g['name'] == gname:
                    gk = g['key']
                    break
            if gk is None and gname == '汉族民间小调':
                gk = 'han'
            if not gk:
                continue
            p = _cache_path(gk)
            if not p.exists():
                continue
            lib_removed = set()
            with suppress(Exception):
                lib_removed = {str(x).strip() for x in (json.loads(p.read_text(encoding='utf-8')).get('artists_removed') or []) if str(x).strip()}
            cc['removed'] = [x for x in (cc.get('removed') or []) if x in lib_removed]
    return v

@app.route('/api/ui/state', methods=['POST'])
def api_ui_state_save():
    """写入 UI 态。body 形如 {'d': {key: 值}, 't': {key: 时间戳}},
    时间戳缺省取当前时刻; 已有项按「新者胜」覆盖, 避免旧值碾掉刚刚的拖动结果。"""
    body = request.get_json(force=True) or {}
    if not isinstance(body, dict):
        return jsonify({'error': '需要 JSON 对象'}), 400
    data = body.get('d') if isinstance(body.get('d'), dict) else body
    ts = body.get('t') if isinstance(body.get('t'), dict) else {}
    now_ms = int(time.time() * 1000)
    cur = _ui_state_load()
    n = 0
    for k, v in data.items():
        key = str(k)
        if not _UI_KEY_OK(key):
            continue
        t = _sane_ui_ts(ts.get(key, now_ms), now_ms)
        prev = cur.get(key)
        if prev is not None and t < _ui_ts_floor(prev.get('t'), now_ms):
            continue                      # 服务端这份更新, 不回退
        if key == 'cm_ethnic_custom' and isinstance(v, dict):
            v = _sanitize_eth_custom(v)   # 移除名单以库文件为准, 拦截旧名单反向污染
        cur[key] = {'v': v, 't': t}
        n += 1
    if n:
        _ui_state_save(cur)
    return jsonify({'ok': True, 'written': n, 'keys': len(cur)})


def _UI_KEY_OK(k):
    """白名单校验: 只持久化已知的 UI 态键, 防止把任意内容塞进持久文件"""
    import re as _re
    return bool(_re.match(r'^cm_(order_ethcards|order_ethrows_[\w\u4e00-\u9fff]{1,24}|my_playlists|pl_removed|ethnic_custom|cfg_v5)$', k))


@app.route('/api/ui/state', methods=['DELETE'])
def api_ui_state_clear():
    """清空某批 key: body 为 key 名数组, 缺省清空全部"""
    data = request.get_json(force=True, silent=True) or {}
    cur = _ui_state_load()
    for k in (data.get('keys') if isinstance(data, dict) else None) or list(cur.keys()):
        cur.pop(k, None)
    _ui_state_save(cur)
    return jsonify({'ok': True, 'keys': len(cur)})


@app.route('/api/ethnos/export')
def api_ethnos_export():
    """协作导出: md=人类可读清单, 缺省group返回目录页"""
    group = request.args.get('group')
    if not group:
        links = ''.join(f'<li><a href="/api/ethnos/export?group={g["key"]}">{g["name"]}</a> — {_cache_path(g["key"]).name}</li>' for g in ETHNIC_GROUPS if _cache_path(g['key']).exists())
        links += '<li><a href="/api/ethnos/export?group=han">汉族民间小调</a> — han_汉族民间小调.json</li>'
        return Response(f'<meta charset="utf-8"><h2>民族歌单导出目录</h2><ol>{links}</ol>', content_type='text/html; charset=utf-8')
    if group not in ETHNIC_BY_KEY and group != 'han':
        return jsonify({'error': '未知的民族'}), 400
    path = _cache_path(group)
    if not path.exists():
        return jsonify({'error': '歌单不存在'}), 404
    payload = json.loads(path.read_text(encoding='utf-8'))
    lines = [f"# {payload.get('group')} · 音乐歌单", '', f"> 共 **{payload.get('count', len(payload.get('tracks') or []))} 首** · 导出时间 {time.strftime('%Y-%m-%d %H:%M')}", '']
    aa, ar = payload.get('artists_added') or [], payload.get('artists_removed') or []
    if aa or ar:
        lines += ['## 歌手增删名单', '']
        if aa: lines.append('- 手动添加: ' + '、'.join(aa))
        if ar: lines.append('- 手动移除: ' + '、'.join(ar))
        lines.append('')
    lines += ['## 曲目清单', '', '| # | 歌名 | 歌手 | 专辑 | 时长 | 音源 |', '|---|------|------|------|------|------|']
    for i, t in enumerate(payload.get('tracks') or [], 1):
        lines.append(f"| {i} | {str(t.get('song_name','')).replace('|','/')[:60]} | {str(t.get('singers','')).replace('|','/')} | {str(t.get('album','')).replace('|','/')[:24]} | {t.get('duration') or ''} | {t.get('source_cn') or t.get('source','')} |")
    return Response('\n'.join(lines), content_type='text/markdown; charset=utf-8')


@app.route('/api/ethnos')
def api_ethnos():
    with ethnos_lock:
        states = dict(ethnos_state)
        building = any(v['state'] == 'building' for v in states.values())
    groups = [{**{'key': g['key'], 'name': g['name'], 'ci': g['ci']}, **states[g['key']]} for g in ETHNIC_GROUPS]
    done = sum(1 for v in states.values() if v['state'] == 'done')
    han_path = ETHNOS_CACHE_DIR / 'han_汉族民间小调.json'
    with suppress(Exception):
        _han_cnt = json.loads(han_path.read_text(encoding='utf-8')).get('count', 0) if han_path.exists() else 0
    han_done = han_path.exists() and _han_cnt > 0
    return jsonify({'groups': groups, 'building': building, 'done': done, 'total': len(ETHNIC_GROUPS), 'jingpo': dict(jingpo_state),
                    'han': {'state': HAN_BUILD_STATE['state'], 'count': HAN_BUILD_STATE['count'] or _han_cnt, 'progress': HAN_BUILD_STATE['progress'], 'done': han_done}})


# 按 album 聚合的虚拟歌单(YouTube/电台合辑)。原先写死只认景颇族, 导致合集无法像歌手那样搬库;
# 现改为"谁的曲目挂着这个专辑名, 合集就算谁的", 移动/隐藏/硬删三条口径与歌手对齐。
_YT_COLL_ALBUMS = {'Kachin old song合集', 'Yup Seng Ing 合辑', 'Ginra 电台合辑'}
_JINGPO_COLL_NAMES = _YT_COLL_ALBUMS   # 旧名保留, 兼容外部引用
INDEX_CACHE = {'sig': None, 'data': None, 'computing': False}
INDEX_SNAPSHOT_PATH = ETHNOS_CACHE_DIR / 'index_snapshot.json'


def _ethnos_files_sig():
    """56 个歌单文件的轻量签名(文件名+mtime+大小), stat 一次毫秒级, 作为索引缓存失效依据"""
    sig = []
    for key in [g['key'] for g in ETHNIC_GROUPS] + ['han']:
        p = _cache_path(key)
        with suppress(OSError):
            stt = p.stat()
            sig.append([p.name, stt.st_mtime_ns, stt.st_size])
            continue
        sig.append([p.name, 0, 0])
    for p in sorted(ETHNOS_CACHE_DIR.glob('t*_*.json')):   # 专题库一并纳入签名, 改动同样触发前端刷新
        with suppress(OSError):
            stt = p.stat()
            sig.append([p.name, stt.st_mtime_ns, stt.st_size])
    return sig


def _compute_index_data():
    """全量扫描56民族歌单 -> 索引数据(歌手/歌单/统计), 单遍完成(含 YouTube 合集)
    合集不再绑定死「景颇族」: 哪个民族的曲目挂着合集专辑名, 合集就归哪个民族 —— 这样才能像歌手一样搬库。
    注: 专辑 Tab 已下线 —— 专辑数据脏(B站源的 album 字段实为 BV 号, top400 里 88 个是 BV 号),
    故不再聚合 albums。artists 聚合必须保留: 首页「民族」Tab 完全建立在它之上。"""
    artists = {}
    songs_total = 0
    coll_stats = {}       # (民族, 合集名) -> {'n': 曲目数, 'cover': 封面}
    group_songs = {}      # 民族 -> 磁盘实际曲目数(供前端卡片显示, 与民族音乐专区同一口径)
    removed_map = {}      # 民族 -> 该民族被 UI 移除的歌手名集合(下沉到后端, 前端过滤仅作兜底)
    coll_removed = {}     # 民族 -> 该民族被隐藏的合集名(合集与歌手同权, 隐藏也下沉到后端)

    def _acc(nm, group, t):
        a = artists.setdefault(nm, {'name': nm, 'group': group, 'count': 0, 'cover': '', 'groups': {}})
        a['count'] += 1
        a['groups'][group] = a['groups'].get(group, 0) + 1
        a['cover'] = a['cover'] or (t.get('cover_url') or '')

    for group, payload in _iter_ethnos_payload():
        removed_map[group] = {str(x).strip() for x in (payload.get('artists_removed') or []) if str(x).strip()}
        coll_removed[group] = {str(x).strip() for x in (payload.get('collections_removed') or []) if str(x).strip()}
        _tracks = payload.get('tracks') or []
        group_songs[group] = len(_tracks)
        for t in _tracks:
            songs_total += 1
            al = t.get('album') or ''
            if al in _YT_COLL_ALBUMS and al not in coll_removed[group]:
                cs = coll_stats.setdefault((group, al), {'n': 0, 'cover': ''})
                cs['n'] += 1
                cs['cover'] = cs['cover'] or (t.get('cover_url') or '')
            names = _split_singers(t.get('singers'))
            if not names:
                continue
            for nm in names:
                if nm in removed_map[group]:
                    continue      # 该歌手已被从此民族移除: 不再出现在卡片/目录里(曲目本身保留)
                _acc(nm, group, t)
    # 歌手归属其出现的「每一个」民族, 不再强行归并到出现次数最多的单一民族。
    # 旧逻辑会把小民族(珞巴族等)被通用歌手名(未知歌手/网络歌手/群星)或跨民族上传者
    # 抽干 —— 例如珞巴族导入的 B站曲目歌手多为上传者名, 这些名字在其它民族也大量出现,
    # 导致珞巴族民族卡片只显示 65 首而非真实的 383 首。现改为每个歌手在其出现过的每个民族
    # 都各计一次, 民族卡片/歌手目录才能反映真实规模。权威归类(ETHNIC_CONFIRMED)的歌手
    # 仍只保留指定民族。
    artist_list = []
    for nm, a in artists.items():
        if nm in ETHNIC_CONFIRMED:
            grp_counts = [(ETHNIC_CONFIRMED[nm], a['count'])]
        else:
            grp_counts = list(a['groups'].items())
        for g, c in grp_counts:
            artist_list.append({'name': nm, 'group': g, 'count': c, 'cover': a['cover'], 'confirmed': nm in ETHNIC_CONFIRMED})
    artist_list.sort(key=lambda x: (-x['count'], x['name']))
    playlists = []
    for g in ETHNIC_GROUPS:
        st = ethnos_state.get(g['key'], {})
        # 歌单 count 以磁盘实际文件为准(外部脚本直接改盘后内存态会滞后, 避免列表数字失真);
        # 主循环已逐文件统计过曲目数, 这里直接复用, 省掉 56 次二次读盘
        _cnt = group_songs.get(g['name'], 0)
        if not _cnt:
            _cnt = st.get('count', 0)
        if st.get('state') == 'done' or _cnt > 0:
            playlists.append({'id': g['key'], 'platform': 'ethnos', 'name': f"{g['name']} · 民族音乐", 'group': g['name'], 'count': _cnt, 'kind': '民族歌单'})
    # 汉族民间小调 (第 56 个民族歌单, 与民族索引信息对应)
    with suppress(Exception):
        _hc = group_songs.get('汉族民间小调', 0)
        if _hc > 0:
            playlists.append({'id': 'han', 'platform': 'ethnos', 'name': '汉族 · 民族音乐', 'group': '汉族民间小调', 'count': _hc, 'kind': '民族歌单'})
    # YouTube/电台合集 (独立歌单入口, 带封面) — 主循环已顺带统计, 无需二次全量扫描。
    # 每条自带 group: 曲目在哪个民族库里, 合集就归哪个民族(搬库后自动出现在目标库)。
    with suppress(Exception):
        for (_g, al), cs in sorted(coll_stats.items(), key=lambda kv: -kv[1]['n']):
            playlists.insert(min(4, len(playlists)), {'id': 'ytcoll:' + al, 'platform': 'index', 'name': al, 'group': _g, 'count': cs['n'], 'kind': 'YouTube合集', 'cover': cs['cover']})
    for grp in PRESET_PLAYLISTS:
        for it in grp['items']:
            pk = 'qq_toplist' if (grp['platform'] == 'qq' and it.get('kind') == 'toplist') else grp['platform']
            playlists.append({'id': str(it['id']), 'platform': pk, 'name': it.get('desc') or it['name'], 'group': grp['platform_name'], 'count': None, 'kind': grp['platform_name']})
    return {'artists': artist_list, 'playlists': playlists, 'group_songs': group_songs,
            'removed': {g: sorted(v) for g, v in removed_map.items() if v},
            'collections_removed': {g: sorted(v) for g, v in coll_removed.items() if v},
            'stats': {'artists': len(artists), 'songs': songs_total,
                      'ethnos_done': sum(1 for v in ethnos_state.values() if v['state'] == 'done'),
                      'ethnos_playlists': sum(1 for _p in playlists if _p['kind'] == '民族歌单')}}


def _save_index_snapshot(sig, data):
    with suppress(Exception):
        tmp = INDEX_SNAPSHOT_PATH.with_suffix('.json.tmp')
        tmp.write_text(json.dumps({'sig': sig, 'data': data}, ensure_ascii=False), encoding='utf-8')
        tmp.replace(INDEX_SNAPSHOT_PATH)


def _recompute_index():
    try:
        data = _compute_index_data()
        sig = _ethnos_files_sig()
        INDEX_CACHE.update(sig=sig, data=data)
        _save_index_snapshot(sig, data)
    finally:
        INDEX_CACHE['computing'] = False


@app.route('/api/topics')
def api_topics():
    """专题曲库列表(索引页「专题」tab): webui/ethnos_cache/tNN_名称.json, 与 56 民族互不干扰"""
    items = []
    for p in sorted(ETHNOS_CACHE_DIR.glob('t*_*.json')):
        m = re.match(r'^(t\d+)_(.+)$', p.stem)
        if not m or not re.fullmatch(r't\d+', m.group(1)):
            continue
        with suppress(Exception):
            doc = json.loads(p.read_text(encoding='utf-8'))
            if doc.get('v') != ETHNOS_SCHEMA:
                continue
            trs = doc.get('tracks') or []
            cover = next((t.get('cover_url') for t in trs if t.get('cover_url')), '')
            items.append({'key': m.group(1), 'name': doc.get('group') or m.group(2),
                          'count': doc.get('count', len(trs)), 'cover': cover})
    items.sort(key=lambda x: x['key'])
    return jsonify({'topics': items})


@app.route('/api/index/sig')
def api_index_sig():
    """索引签名(轻量): 库文件有任何增删改都会变。前端据此决定要不要重拉 /api/index,
    避免"后台改了数据、前端还显示旧的"这种看起来像 bug 的错觉。"""
    try:
        sig = _ethnos_files_sig()
        return jsonify({'sig': str(hash(str(sig))), 'n': len(sig)})
    except Exception:
        return jsonify({'sig': '', 'n': 0})


@app.route('/api/index')
def api_index():
    """全局音乐库索引: 文件签名未变直接用缓存/快照(毫秒级); 变了先给旧数据、后台重算"""
    sig = _ethnos_files_sig()
    if INDEX_CACHE['data'] is not None and INDEX_CACHE['sig'] == sig:
        return jsonify(INDEX_CACHE['data'])
    snap = None
    with suppress(Exception):
        if INDEX_SNAPSHOT_PATH.exists():
            snap = json.loads(INDEX_SNAPSHOT_PATH.read_text(encoding='utf-8'))
    if snap and snap.get('sig') == sig:
        INDEX_CACHE.update(sig=sig, data=snap['data'])
        return jsonify(snap['data'])
    if snap and snap.get('data'):
        if not INDEX_CACHE['computing']:
            INDEX_CACHE['computing'] = True
            threading.Thread(target=_recompute_index, daemon=True).start()
        return jsonify(snap['data'])
    data = _compute_index_data()
    cur_sig = _ethnos_files_sig()
    INDEX_CACHE.update(sig=cur_sig, data=data)
    threading.Thread(target=_save_index_snapshot, args=(cur_sig, data), daemon=True).start()
    return jsonify(data)


# ---------------------------------------------------------------- 曲库歌名索引 (本地曲库歌曲检索)
# 索引载荷 3.88MB 且不含 song_name, 无法前端检索 -> 服务端建精简曲库索引, 结果注册进常驻会话就地可播。
SONG_IDX_KEEP = {'source', 'root_source', 'song_name', 'singers', 'album', 'ext', 'file_size',
                 'file_size_bytes', 'duration_s', 'duration', 'bitrate', 'cover_url', 'download_url',
                 'default_download_headers', 'default_download_cookies', 'protocol', 'identifier', 'work_dir'}
# 剔除 raw_data(10.7KB/条) / download_url_status(与 download_url 重复) / lyric(缺失时回落在线搜词)
SONG_IDX_PAGE, SONG_IDX_MAX, SONG_IDX_MIN_Q = 60, 200, 2
SONG_IDX_SCAN_CAP = 4000      # 常驻会话 items 上限, 超出清空重建
SONG_Q_TTL, SONG_Q_MAX = 300.0, 64
SONG_IDX = {'sig': None, 'recs': [], 'blobs': [], 'building': False, 'built_at': 0.0}
SONG_IDX_LOCK = threading.Lock()
SONG_Q_CACHE = OrderedDict()  # 查询结果 LRU(不含 sid: sid 常驻稳定)
SONG_SID = [None]             # 常驻索引会话 sid


def _song_blob(song_data):
    """精简 _song 为可回炉的 JSON 串"""
    return json.dumps({k: v for k, v in (song_data or {}).items()
                       if k in SONG_IDX_KEEP and v not in (None, '', {}, [])},
                      ensure_ascii=False, separators=(',', ':'))


def _build_song_index():
    try:
        if INDEX_CACHE.get('computing'):
            time.sleep(3)      # 给索引重算让路, 避免两个 643MB 全量扫描叠加打满内存
        recs, blobs, gi = [], [], {}
        for group, t in _iter_ethnos_tracks():
            if not (t.get('previewable') or t.get('downloadable')):
                continue       # 不可播也不可下的坏条目不入库(播放会 502)
            gi[group] = gi.get(group, -1) + 1
            rec = {k: v for k, v in t.items() if k != '_song'}
            rec['k'] = _core_name(t.get('song_name'))       # 主匹配键(去括号/版本后缀)
            rec['kr'] = _norm_str(t.get('song_name'))       # 兜底匹配键(完整归一化)
            rec['sk'] = _norm_str(t.get('singers'))
            rec['ak'] = _norm_str(t.get('album'))
            rec['g'] = group
            rec['gi'] = gi[group]                            # 族内序号, 与 g 构成确定性 locator
            recs.append(rec)
            blobs.append(_song_blob(t.get('_song')))
        with SONG_IDX_LOCK:
            SONG_IDX.update(sig=_ethnos_files_sig(), recs=recs, blobs=blobs,
                            built_at=time.time(), building=False)
    except Exception:
        with SONG_IDX_LOCK:
            SONG_IDX['building'] = False


def _song_index_state():
    """返回 (recs, blobs); 尚未就绪返回 None(已触发后台构建)"""
    sig = _ethnos_files_sig()
    with SONG_IDX_LOCK:
        if SONG_IDX['recs'] and SONG_IDX['sig'] == sig:
            return SONG_IDX['recs'], SONG_IDX['blobs']
        if not SONG_IDX['building']:
            SONG_IDX['building'] = True
            threading.Thread(target=_build_song_index, daemon=True).start()
        return (SONG_IDX['recs'], SONG_IDX['blobs']) if SONG_IDX['recs'] else None


def _song_index_search(recs, blobs, nq, group, limit, offset):
    """返回 (pairs, total); pairs = [(rec, blob), ...]"""
    hits = []
    for i, r in enumerate(recs):
        if group and r['g'] != group:
            continue
        k, kr = r['k'], r['kr']
        if nq == k or nq == kr:
            sc = 1000                                        # 完全相等
        elif k.startswith(nq) or kr.startswith(nq):
            sc = 700                                         # 前缀
        elif nq in k or nq in kr:
            sc = 500                                         # 子串
        elif r['sk'] and (nq == r['sk'] or nq in r['sk']):
            sc = 300                                         # 歌手兜底
        elif r['ak'] and not r['ak'].startswith('bv') and nq in r['ak']:
            sc = 200                                         # 专辑兜底(B站源 album 实为 BV 号, 排除)
        else:
            continue
        hits.append((sc, i, r))
    if not hits:
        return [], 0
    # 有歌名命中就丢弃纯歌手/专辑命中, 避免"搜歌名却返回该歌手全部歌"
    if max(h[0] for h in hits) >= 500:
        hits = [h for h in hits if h[0] >= 500]
    hits.sort(key=lambda x: (-(x[0] + (200 if x[2].get('previewable') else 0)
                                    + (50 if x[2].get('downloadable') else 0)),
                             len(x[2]['k']), -(x[2].get('duration_s') or 0)))
    seen, pairs, total = set(), [], 0
    for sc, i, r in hits:
        # 同一民歌常被 B站/YouTube/咪咕多源重复收录: 按 歌名+首位歌手 去重, 留最优(可播)那份
        s0 = _norm_str(_first_singer(r.get('singers'))) or _norm_str(r.get('singers'))
        dk = (r['k'] or r['kr']) + '|' + s0
        if dk in seen:
            continue
        seen.add(dk)
        total += 1
        if offset <= total - 1 < offset + limit:
            pairs.append((r, blobs[i]))
    return pairs, total


def _song_session_sid():
    """常驻索引会话: 全局唯一 sid。不用每次 new_session —— search_sessions 上限仅 16,
    每次检索新建会把用户正在播放的会话挤掉(表现为止播/404)。"""
    # 注意: new_session() 内部也会获取 state_lock, 而 state_lock 是不可重入的普通 Lock,
    # 因此必须在「未持锁」状态下调用, 否则死锁(整个服务所有请求挂起)。
    sid = SONG_SID[0]
    items = None
    with state_lock:
        if sid and sid in search_sessions:
            items = search_sessions[sid]['items']
    if items is not None:
        if len(items) > SONG_IDX_SCAN_CAP:
            with state_lock:
                items.clear()
        return sid
    new_sid = new_session(_ethnos_get_client())
    with state_lock:
        SONG_SID[0] = new_sid
    return new_sid


def _finalize_song_search(q, group, pairs):
    from musicdl.modules import SongInfo as _SongInfo
    sid = _song_session_sid()
    items = search_sessions[sid]['items']
    tracks = []
    for r, blob in pairs:
        key = f'Idx#{r["g"]}#{r["gi"]}'          # 确定性 key, 多次检索/重建间稳定
        if key not in items:
            with suppress(Exception):
                items[key] = _SongInfo.fromdict(json.loads(blob))
        t = {k: v for k, v in r.items() if k not in ('k', 'kr', 'sk', 'ak', 'g', 'gi')}
        t['id'], t['sid'] = key, sid
        t['group'] = r['g']                       # 所属民族库(前端展示「民族库」列/删除定位)
        tracks.append(t)                          # 天然不含 _song, 与 _finalize_virtual_playlist 口径一致
    return {'id': f'songsearch:{q}', 'name': f'搜索「{q}」',
            'cover': (tracks[0].get('cover_url') if tracks else ''),
            'count': len(tracks), 'tracks': tracks,
            'platform': 'index', 'platform_name': '本地曲库 · 歌名检索', 'sid': sid}


@app.route('/api/index/search')
def api_index_search():
    """本地曲库歌名检索(就地可播)"""
    q = (request.args.get('q') or '').strip()
    group = (request.args.get('group') or '').strip()
    try:
        limit = max(1, min(int(request.args.get('limit') or SONG_IDX_PAGE), SONG_IDX_MAX))
        offset = max(0, int(request.args.get('offset') or 0))
    except Exception:
        limit, offset = SONG_IDX_PAGE, 0
    nq = _norm_str(q)
    if len(nq) < SONG_IDX_MIN_Q:
        return jsonify({'building': False, 'q': q, 'group': group, 'total': 0, 'count': 0,
                        'capped': False, 'tracks': [], 'sid': SONG_SID[0],
                        'hint': f'请至少输入 {SONG_IDX_MIN_Q} 个字'})
    ck = f'{nq}|{group}|{limit}|{offset}'
    hit = SONG_Q_CACHE.get(ck)
    if hit and time.time() - hit['at'] < SONG_Q_TTL:
        SONG_Q_CACHE.move_to_end(ck)
        return jsonify(hit['payload'])
    idx = _song_index_state()
    if idx is None:
        return jsonify({'building': True, 'q': q, 'group': group, 'total': 0, 'count': 0,
                        'capped': False, 'tracks': [], 'sid': None,
                        'hint': '首次检索需建立曲库索引，请稍候…'}), 202
    recs, blobs = idx
    pairs, total = _song_index_search(recs, blobs, nq, group, limit, offset)
    if pairs:
        payload = dict(_finalize_song_search(q, group, pairs), building=False, q=q, group=group,
                       total=total, capped=total > offset + limit)
    else:
        payload = {'building': False, 'q': q, 'group': group, 'total': total, 'count': 0,
                   'capped': False, 'tracks': [], 'sid': SONG_SID[0],
                   'hint': '没有匹配的歌曲，换个关键词试试'}
    SONG_Q_CACHE[ck] = {'at': time.time(), 'payload': payload}
    while len(SONG_Q_CACHE) > SONG_Q_MAX:
        SONG_Q_CACHE.popitem(last=False)
    return jsonify(payload)


@app.route('/api/ethnos/build', methods=['POST'])
def api_ethnos_build():
    data = request.get_json(force=True)
    action = data.get('action') or 'build'
    if action == 'stop':
        ethnos_stop.set()
        return jsonify({'ok': True})
    ethnos_stop.clear()
    if action == 'rebuild_all':
        # 强制重建: 忽略done状态(用于恢复被换血的民族), 增量语义下老歌全保留
        def _rebuild_all():
            # 未恢复的民族(纯B站换血版或无B站增量)优先
            def _needs_recover(key):
                path = _cache_path(key)
                with suppress(Exception):
                    dd = json.loads(path.read_text(encoding='utf-8'))
                    bili = sum(1 for t in dd.get('tracks') or [] if t.get('source') == 'Bilibili')
                    domestic = len(dd.get('tracks') or []) - bili
                    if domestic < 50:
                        return 0   # 最优先: 国内曲目缺失
                return 1
            pending2 = sorted([g['key'] for g in ETHNIC_GROUPS], key=_needs_recover)
            def consume2():
                while True:
                    if ethnos_stop.is_set():
                        return
                    try:
                        key = pending2.pop(0)
                    except IndexError:
                        return
                    with ethnos_lock:
                        ethnos_state[key]['state'] = 'idle'
                    build_ethnos_playlist(key)
                    time.sleep(4)
            ws = [threading.Thread(target=consume2, daemon=True) for _ in range(2)]
            [w.start() for w in ws]
            [w.join() for w in ws]
        threading.Thread(target=_rebuild_all, daemon=True).start()
        return jsonify({'ok': True, 'mode': 'rebuild_all'})
    if action == 'han':
        threading.Thread(target=build_han_folk, daemon=True).start()
        return jsonify({'ok': True, 'mode': 'han'})
    if action == 'bili':
        group = data.get('group')
        if group not in ETHNIC_BY_KEY:
            return jsonify({'error': '未知的民族'}), 400
        threading.Thread(target=build_bilibili_ethnos, args=(group,), daemon=True).start()
        return jsonify({'ok': True, 'mode': 'bili'})
    if action == 'bili_all':
        threading.Thread(target=bilibili_build_all_worker, daemon=True).start()
        return jsonify({'ok': True, 'mode': 'bili_all'})
    if action == 'jingpo':
        threading.Thread(target=build_jingpo_deep, daemon=True).start()
        return jsonify({'ok': True, 'mode': 'jingpo'})
    if action == 'jingpo_yt':
        threading.Thread(target=build_jingpo_youtube_patch, daemon=True).start()
        return jsonify({'ok': True, 'mode': 'jingpo_yt'})
    if action == 'build_all':
        threading.Thread(target=ethnos_build_all_worker, daemon=True).start()
        return jsonify({'ok': True, 'mode': 'all'})
    group = data.get('group')
    if group not in ETHNIC_BY_KEY:
        return jsonify({'error': '未知的民族'}), 400
    with ethnos_lock:
        if ethnos_state[group]['state'] == 'building':
            return jsonify({'ok': True, 'mode': 'building'})
    threading.Thread(target=ethnos_build_worker, args=(group,), daemon=True).start()
    return jsonify({'ok': True, 'mode': 'one'})


# ---------------------------------------------------------------- 下载 (多音源)
@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.get_json(force=True)
    sid, picks = data.get('sid'), data.get('picks') or []
    with state_lock:
        session = search_sessions.get(sid)
    if not session:
        return jsonify({'error': '会话已过期,请重新搜索'}), 400
    submitted = []
    for pick in picks:
        item_id = pick.get('id') if isinstance(pick, dict) else pick
        song = session['items'].get(item_id)
        if song is None:
            continue
        tid = uuid.uuid4().hex[:8]
        with state_lock:
            tasks[tid] = {
                'id': tid, 'name': song.song_name or '未知曲目', 'singer': song.singers or '—',
                'source': SOURCE_NAMES.get((song.source or '').removesuffix('MusicClient'), (song.source or '').removesuffix('MusicClient')),
                'ext': (song.ext or '').lstrip('.').upper(), 'file_size': song.file_size or '',
                'status': 'queued', 'file': None, 'error': None, 'created_at': time.time(), 'finished_at': None,
            }
        download_pool.submit(download_worker, tid, session['client'], song)
        submitted.append(tid)
    if not submitted:
        return jsonify({'error': '没有可下载的曲目'}), 400
    with state_lock:
        while len(tasks) > 200:
            tasks.popitem(last=False)
    return jsonify({'task_ids': submitted})


@app.route('/api/tasks')
def api_tasks():
    with state_lock:
        items = list(tasks.values())[::-1]
    active = any(t['status'] in ('queued', 'downloading') for t in items)
    return jsonify({'tasks': items, 'active': active})


@app.route('/api/tasks/clear', methods=['POST'])
def api_tasks_clear():
    with state_lock:
        for tid in [k for k, v in tasks.items() if v['status'] in ('done', 'failed')]:
            tasks.pop(tid, None)
    return jsonify({'ok': True})


# ---------------------------------------------------------------- 本地音乐库
@app.route('/api/files')
def api_files():
    files = []
    for path in DOWNLOAD_DIR.rglob('*'):
        if path.is_file() and path.suffix.lower() not in {'.pkl'} and not path.name.startswith('.'):
            st = path.stat()
            files.append({
                'name': path.stem, 'file': path.name, 'path': str(path.relative_to(DOWNLOAD_DIR)),
                'ext': path.suffix.lstrip('.').upper(),
                'size': f'{st.st_size / 1024 / 1024:.1f}MB' if st.st_size > 1024 * 1024 else f'{st.st_size / 1024:.0f}KB',
                'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime)),
                'playable': path.suffix.lower() in AUDIO_EXTS,
            })
    files.sort(key=lambda x: x['mtime'], reverse=True)
    return jsonify({'files': files[:400]})


@app.route('/files/<path:p>')
def serve_file(p):
    return send_from_directory(DOWNLOAD_DIR, p, conditional=True)


@app.route('/api/file/delete', methods=['POST'])
def api_file_delete():
    data = request.get_json(force=True)
    target = (DOWNLOAD_DIR / (data.get('path') or '')).resolve()
    if not str(target).startswith(str(DOWNLOAD_DIR.resolve())) or target == DOWNLOAD_DIR.resolve():
        return jsonify({'error': '非法路径'}), 400
    if target.is_file():
        target.unlink()
        with suppress(Exception):
            shutil.rmtree(target.with_suffix('.pkl'), ignore_errors=True)
    return jsonify({'ok': True})


@app.route('/api/open_folder', methods=['POST'])
def api_open_folder():
    data = request.get_json(force=True)
    target = (DOWNLOAD_DIR / (data.get('path') or '')).resolve()
    if not str(target).startswith(str(DOWNLOAD_DIR.resolve())):
        return jsonify({'error': '非法路径'}), 400
    folder = target if target.is_dir() else target.parent
    subprocess.Popen(['open', str(folder)])
    return jsonify({'ok': True})


@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    threading.Timer(1.0, os._exit, (0,)).start()
    return jsonify({'ok': True})


if __name__ == '__main__':
    print(f'[mountainriverechoes] serving at http://{HOST}:{PORT}  (downloads -> {DOWNLOAD_DIR})', flush=True)
    # 预热曲库歌名索引(后台, 约 10-15s), 让用户首次搜歌不必等建索引
    threading.Timer(5.0, lambda: threading.Thread(target=_build_song_index, daemon=True).start()).start()
    # [2026-10-03 关掉] 启动后台过期签名链重抓: _same_recording 安全阀下几乎 0 首成功,
    # 反而拖慢启动 5 秒 + 占 worker 线程。详见 LINK_REFRESH_* 上方注释。
    # import queue as _q
    # queue_module = _q
    # threading.Timer(5.0, lambda: threading.Thread(target=_start_link_refresh_worker, daemon=True).start()).start()
    # 启动时若仍有未完成的民族歌单, 自动后台续建
    with ethnos_lock:
        _pending = sum(1 for v in ethnos_state.values() if v['state'] != 'done')
    if _pending:
        # [2026-09-28] 临时关闭启动自动全量构建: 它会占用全局单例客户端并与 bili_all 抢
        # building 状态、且 domestic 重建会把 ytdlp 通道的 B站/YouTube 曲目挤出。
        # 改为手动触发。需要自动续建时取消下一行注释即可。
        print(f'[mountainriverechoes] skip auto build-all ({_pending} groups pending); trigger manually', flush=True)
        # threading.Timer(5.0, lambda: threading.Thread(target=ethnos_build_all_worker, daemon=True).start()).start()
    with suppress(Exception):
        _han = ETHNOS_CACHE_DIR / 'han_汉族民间小调.json'
        if _han.exists():
            _hp = json.loads(_han.read_text(encoding='utf-8'))
            if _hp.get('partial'):
                print('[mountainriverechoes] resuming han folk build', flush=True)
                threading.Timer(8.0, lambda: threading.Thread(target=build_han_folk, daemon=True).start()).start()
    app.run(host=HOST, port=PORT, threaded=True)