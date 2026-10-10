#!/usr/bin/env bash
# 核 <base>..<head> 区间内每个提交的作者与提交者邮箱都在 noreply 域，否则退出 1。
# 允许：<id>+<login>@users.noreply.github.com，以及 GitHub 网页/合并生成的 noreply@github.com。
# 用法：scripts/check-commit-emails.sh <base> <head>；base 为空或全 0 时检查 head 的全部历史。
set -euo pipefail

base=${1:-}
head=${2:?用法：check-commit-emails.sh <base> <head>}

if [[ -z $base || $base =~ ^0+$ ]]; then
  range=$head
else
  range=$base..$head
fi

allowed='^([0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com|noreply@github\.com)$'
bad=0
while IFS='|' read -r sha ae ce; do
  if [[ ! $ae =~ $allowed ]]; then
    echo "::error::提交 $sha 作者邮箱不是 noreply：$ae"
    bad=1
  fi
  if [[ ! $ce =~ $allowed ]]; then
    echo "::error::提交 $sha 提交者邮箱不是 noreply：$ce"
    bad=1
  fi
done < <(git log --format='%h|%ae|%ce' "$range")

count=$(git rev-list --count "$range")
if [[ $bad == 1 ]]; then
  echo "共检查 $count 个提交，发现非 noreply 邮箱"
  exit 1
fi
echo "共检查 $count 个提交，邮箱均为 noreply"
