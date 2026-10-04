#!/bin/bash
# 景颇库迁移看护: 驱动每轮退出后等 30 分钟重跑, 直到「所有歌手已排查完毕」或超 48 次(24h)
cd "$(dirname "$0")"
for i in $(seq 1 48); do
  ./venv/bin/python resong_singer.py >> /tmp/resong.log 2>&1
  if tail -8 /tmp/resong.log | grep -q "所有歌手已排查完毕"; then
    echo "[$(date '+%H:%M:%S')] 看护: 迁移全部完成, 退出" >> /tmp/resong.log
    exit 0
  fi
  echo "[$(date '+%H:%M:%S')] 看护: 第 $i 次驱动退出, 30 分钟后重试" >> /tmp/resong.log
  sleep 1800
done
echo "[$(date '+%H:%M:%S')] 看护: 24 小时上限, 退出" >> /tmp/resong.log
