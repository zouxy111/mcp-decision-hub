#!/usr/bin/env sh
# mcp-decision-hub · Skill 安装脚本
#
#   curl -fsSL https://hub.tdp-demo.work/static/skills/install.sh | sh
#   curl -fsSL https://hub.tdp-demo.work/static/skills/install.sh | sh -s build-decision-model
#
# 不带参数 = 装全部；带参数 = 只装指定名字（可写多个）。
# 可覆盖：
#   SKILLS_DIR     目标目录，默认 ~/.workbuddy/skills
#   SKILLS_BASE_URL 源地址，默认 https://hub.tdp-demo.work/static/skills
#
# 脚本只做三件事：mkdir、下载、解包。不写别处、不改配置、不联网做别的事。

set -eu

BASE="${SKILLS_BASE_URL:-https://hub.tdp-demo.work/static/skills}"
DIR="${SKILLS_DIR:-$HOME/.workbuddy/skills}"
ALL="connect-decision-hub build-decision-model"

if [ "$#" -eq 0 ]; then
  # shellcheck disable=SC2086
  set -- $ALL
fi

mkdir -p "$DIR"

for name in "$@"; do
  printf '安装 %s ... ' "$name"
  if ! curl -fsSL "$BASE/$name.tar.gz" | tar xz -C "$DIR"; then
    printf '失败\n' >&2
    exit 1
  fi
  if [ -f "$DIR/$name/SKILL.md" ]; then
    printf 'ok\n'
  else
    printf '失败：%s/SKILL.md 没有出现\n' "$name" >&2
    exit 1
  fi
done

printf '\n已安装到 %s：\n' "$DIR"
ls -1 "$DIR"
printf '\n客户端若未刷新，重启一次客户端。\n'
