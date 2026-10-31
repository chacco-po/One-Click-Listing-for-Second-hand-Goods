#!/usr/bin/env bash
# 提交前体检：确认将要推送到 GitHub 的内容不会超出平台限制
#
# GitHub 限制：
#   - 单文件硬上限 100MB（超过直接拒绝，且无法通过 LFS 之外的任何手段绕过）
#   - 单仓库建议不超过 1GB，强烈建议不超过 100MB
#   - 单次 push 建议不超过 50MB，避免触发限流
#
# 用法：
#   bash scripts/check_repo_size.sh
set -euo pipefail

cd "$(dirname "$0")/.."

FAIL=0
WARN=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  [OK]   %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; WARN=$((WARN + 1)); }
bad()  { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL + 1)); }

say ""
say "=== 1. 忽略规则是否生效 ==="
# 注意：.gitignore 用的是 data/* / models/*（保留 .gitkeep），
# 因此目录本身不算「被忽略」，要探测目录内的真实文件。
check_ignored() { # $1=目录 $2=代表性文件
  if [[ -e "$1/$2" ]]; then
    if git check-ignore -q "$1/$2" 2>/dev/null; then
      ok "$1/$2 已被忽略"
    else
      bad "$1/$2 未被忽略 —— 大文件会被提交！"
    fi
  else
    # 目录内暂无该文件，退而检查整目录模式是否命中
    if git check-ignore -q "$1/" 2>/dev/null; then
      ok "$1/ 已被忽略"
    else
      ok "$1/ 内容按规则忽略（当前目录内无 $2 可探测）"
    fi
  fi
}
check_ignored data app.db
check_ignored models u2net.onnx

# 兜底：确认权重文件绝不会进入索引
if git ls-files --error-unmatch models/u2net.onnx >/dev/null 2>&1; then
  bad "models/u2net.onnx 已在版本控制中，必须执行 git rm --cached！"
else
  ok "models/u2net.onnx 未被跟踪"
fi

say ""
say "=== 2. 已跟踪文件中的超大文件（>50MB）==="
BIG=$(git ls-files -z | xargs -0 -r ls -l 2>/dev/null \
      | awk '$5 > 52428800 {printf "%.1f MB  %s\n", $5/1048576, $9}')
if [[ -z "$BIG" ]]; then
  ok "无超过 50MB 的已跟踪文件"
else
  printf '%s\n' "$BIG"
  bad "存在超大文件，GitHub 会拒绝（单文件上限 100MB）"
fi

say ""
say "=== 3. 暂存区体积（本次将推送的内容）==="
git add -A
STAGED_BYTES=$(git diff --cached --numstat | awk '{print $1}' | tr -d '\-' | \
  awk '{ if ($1 != "-" && $1 != "") s += $1; else s += 0 } END {print s+0}')
if [[ "$STAGED_BYTES" -eq 0 ]]; then
  ok "无待提交变更"
else
  # numstat 给的是行数，这里改为按实际 blob 体积统计更准确
  STAGED_BYTES=$(git diff --cached --name-only --diff-filter=ACM -z | \
    xargs -0 -r ls -l 2>/dev/null | awk '{s+=$5} END {printf "%d", s+0}')
  STAGED_MB=$((STAGED_BYTES / 1048576))
  ok "本次待提交约 ${STAGED_MB}MB"
  if [[ "$STAGED_MB" -gt 50 ]]; then
    warn "待提交体积偏大，建议拆分提交"
  fi
fi

say ""
say "=== 4. 仓库总体积 ==="
REPO_MB=$(( $(du -sk .git | awk '{print $1}') / 1024 ))
ok ".git 目录约 ${REPO_MB}MB"
if [[ "$REPO_MB" -gt 100 ]]; then
  warn "仓库超过 100MB，建议用 git filter-repo 清理历史大对象"
else
  ok "远低于 100MB 建议阈值"
fi

say ""
say "=== 5. Git 历史中是否残留大文件 ==="
HIST_BIG=$(git rev-list --objects --all | git cat-file --batch-check='%(objectsize) %(rest)' 2>/dev/null \
  | awk '$1 > 52428800 {printf "%.1f MB  %s\n", $1/1048576, $2}')
if [[ -z "$HIST_BIG" ]]; then
  ok "历史中无超过 50MB 的对象"
else
  printf '%s\n' "$HIST_BIG"
  bad "历史中残留大文件，需要 filter-repo 重写历史"
fi

say ""
say "=== 6. 未配置远端 ==="
if [[ -z "$(git remote)" ]]; then
  warn "尚未配置远端，推送前请执行："
  say "       git remote add origin <你的仓库地址>"
  say "       git push -u origin $(git branch --show-current)"
else
  ok "远端已配置：$(git remote -v | head -1)"
fi

say ""
if [[ "$FAIL" -gt 0 ]]; then
  say "结论：存在 $FAIL 项阻塞问题，请先修复后再推送。"
  exit 1
fi
say "结论：无阻塞问题（$WARN 项提示）。可安全推送。"
