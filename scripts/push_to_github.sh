#!/usr/bin/env bash
# 一键推送到 GitHub（解决网页版上传「.gitignore 已隐藏」的问题）
#
# 问题背景：
#   GitHub 网页端「Add file → Upload files」会主动隐藏 .gitignore，
#   避免上传的忽略规则把同批文件过滤掉。结果是 .gitignore、.gitattributes
#   这类点文件永远传不上去，克隆者拿不到忽略规则，data/ 与 models/ 会污染仓库。
#
# 本脚本：
#   1) 检查远端是否已配置
#   2) 推送前体检（复用 check_repo_size.sh）
#   3) 确认 .gitignore / .gitattributes 等点文件确实在提交中
#   4) 执行 push
#
# 用法：
#   bash scripts/push_to_github.sh https://github.com/<用户名>/<仓库名>.git
#   bash scripts/push_to_github.sh            # 沿用已配置的 origin
set -euo pipefail

cd "$(dirname "$0")/.."

REMOTE_URL="${1:-}"
BRANCH="$(git branch --show-current)"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; CYA=$'\033[36m'; RST=$'\033[0m'
ok()   { printf '  [%sOK%s]   %s\n'   "$GRN" "$RST" "$*"; }
warn() { printf '  [%sWARN%s] %s\n'   "$YLW" "$RST" "$*"; }
bad()  { printf '  [%sFAIL%s] %s\n'   "$RED" "$RST" "$*"; }
info() { printf '  %s\n' "$*"; }

echo ""
echo "=============================================="
echo " 推送到 GitHub"
echo "=============================================="

# ---- 1. 远端配置 ----
if [[ -n "$REMOTE_URL" ]]; then
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$REMOTE_URL"
    ok "origin 已更新为 $REMOTE_URL"
  else
    git remote add origin "$REMOTE_URL"
    ok "origin 已添加：$REMOTE_URL"
  fi
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo ""
  bad "尚未配置远端。请用以下命令重试："
  info "  bash scripts/push_to_github.sh https://github.com/<用户名>/<仓库名>.git"
  echo ""
  exit 1
fi

ok "远端：$(git remote get-url origin)"
ok "分支：$BRANCH"

# ---- 2. 推送前体检 ----
echo ""
info "${CYA}[1/3] 仓库体检${RST}"
if bash scripts/check_repo_size.sh >/tmp/_sizecheck.log 2>&1; then
  ok "体检通过"
else
  bad "体检未通过，明细如下："
  grep -E "FAIL|WARN" /tmp/_sizecheck.log | sed 's/^/       /' || true
  echo ""
  warn "仍要继续推送请确认上面标红的项可接受"
  read -r -p "  继续？(y/N) " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || exit 1
fi

# ---- 3. 关键点文件核查 ----
echo ""
info "${CYA}[2/3] 关键隐藏文件核查${RST}"
MISSING=0
for f in .gitignore .gitattributes backend/__init__.py tests/assets/sample.jpg; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    ok "$f 已在版本控制中"
  else
    bad "$f 未被跟踪 —— 网页端上传会漏掉它"
    MISSING=1
  fi
done

if [[ "$MISSING" -eq 1 ]]; then
  echo ""
  info "正在补加入库…"
  git add -A
  git commit -m "chore: 补齐隐藏文件（.gitignore/.gitattributes/测试素材）" || true
  ok "已补交"
fi

# ---- 4. 推送 ----
echo ""
info "${CYA}[3/3] 推送到远端${RST}"
info "  提示：若弹出浏览器授权，请登录并同意授权 Git Credential Manager"
echo ""

if git push -u origin "$BRANCH"; then
  echo ""
  echo "=============================================="
  ok "推送成功"
  echo "=============================================="
  echo ""
  info "仓库地址：$(git remote get-url origin)"
  echo ""
  info "验证 .gitignore 是否已上线："
  info "  在仓库页面搜索 .gitignore，或直接访问："
  info "  $(git remote get-url origin | sed 's/\.git$//')/blob/$BRANCH/.gitignore"
  echo ""
  info "克隆后他人只需执行："
  info "  bash scripts/install.sh && bash scripts/fetch_models.sh && bash scripts/dev.sh"
else
  echo ""
  bad "推送失败。常见原因："
  info "  1) 远端已有提交（网页端上传过）→ 先合并："
  info "       git pull origin $BRANCH --allow-unrelated-histories"
  info "       git push -u origin $BRANCH"
  info "  2) 认证失败 → 用 Personal Access Token 代替密码，或配置 SSH 密钥"
  info "  3) 分支名不符 → 本地分支 $BRANCH，改为与远端一致的分支名"
  echo ""
  exit 1
fi
