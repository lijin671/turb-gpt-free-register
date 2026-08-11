#!/usr/bin/env bash
# 验证 manymail 域名 DNS 是否可正常收信
# 收信机已实证: 107.174.133.11:25 banner "220 mail.lijin671.com Python SMTP 1.4.6"
# 期望: mail.lijin.ug.cx / lijin.ug.cx 的 A 记录 = 107.174.133.11，且只有一条
# 用法: bash tools/check_domain_dns.sh
set -u
EXPECT_A=107.174.133.11
NS1=111.230.44.144   # ns1.9v4.com

check() {
  local d="$1"
  local a_list mx txt spf_ip
  a_list=$(dig +noall +answer A "$d" @$NS1 | awk '{print $NF}' | sort)
  mx=$(dig +short MX "$d" @$NS1 | head -1)
  txt=$(dig +short TXT "$d" @$NS1 | tr -d '"' | tr '\n' ' ')
  spf_ip=$(echo "$txt" | grep -oE 'ip4:[0-9.]+' | sed 's/ip4://' | head -1)
  echo "=== $d （权威 $NS1 直查） ==="
  echo "  A 记录数: $(echo "$a_list" | grep -c .)"
  echo "$a_list" | sed 's/^/    A: /'
  echo "  MX    = ${mx:-<none>}"
  echo "  TXT   = ${txt:-<none>}"
  if echo "$a_list" | grep -qx "$EXPECT_A" && ! echo "$a_list" | grep -vqx "$EXPECT_A"; then
    echo "  A: PASS（唯一且正确）"
  else
    echo "  A: FAIL（需唯一指向 $EXPECT_A；当前含: $(echo "$a_list" | tr '\n' ' ')）"
  fi
  [ -n "$mx" ] && echo "  MX: PASS" || echo "  MX: FAIL (无 MX)"
  if [ "$spf_ip" = "$EXPECT_A" ]; then
    echo "  SPF: PASS"
  else
    echo "  SPF: WARN (当前 ip4=${spf_ip:-无}，建议 v=spf1 ip4:$EXPECT_A -all)"
  fi
  echo
}

check mail.lijin.ug.cx
check lijin.ug.cx
check mail.lijin671.com
