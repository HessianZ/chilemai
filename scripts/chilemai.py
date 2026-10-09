#!/usr/bin/env python3
"""吃了麦？—— 营养解析 / 筛选 / 菜单对齐

麦当劳 MCP 返回的膳食营养数据不是 JSON，而是一种自定义的定宽列文本；
`query-meals` 返回的菜单名称又与营养库存在差异（如 "纯牛奶(盒装)" vs "纯牛奶（盒装）"）。
本脚本负责把这层脏数据抹平，让上层决策只面对干净的结构化结果。

子命令
    filter   按营养目标筛选 + 排序
    match    把在售菜单与营养库做名称对齐

用法
    cat nutrition.txt | python3 chilemai.py filter --goal low-cal --limit 8
    python3 chilemai.py match --menu menu.json --nutrition nutrition.txt

依赖：仅标准库（Python 3.8+）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

COLUMNS = [
    "productName",
    "nutritionDescription",
    "energyKj",
    "energyKcal",
    "protein",
    "fat",
    "carbohydrate",
    "sodium",
    "calcium",
]
NUMERIC = COLUMNS[2:]

# 目标 -> (上限过滤, 下限过滤, 排序字段, 方向, 默认餐品类型)
# 数值单位：kcal / g / mg，均取自麦当劳官方营养表
#
# 为什么要 kinds：只按热量排序会得到"无糖可乐 / 锡兰红茶"这类零卡饮品——
# 它们是饮料不是饭。按"要低卡"推荐一杯可乐是错的，所以低卡类目标默认只从
# 主食和配餐里挑，饮品与甜品需要用户显式指定。
GOALS: Dict[str, Dict[str, Any]] = {
    "high-protein": {
        "min": {"protein": 20.0},
        "sort": ("protein", "desc"),
        "kinds": ["main"],
    },
    "low-cal": {
        "max": {"energyKcal": 500.0},
        "sort": ("energyKcal", "asc"),
        "kinds": ["main", "side"],
    },
    "low-fat": {
        "max": {"fat": 15.0},
        "sort": ("fat", "asc"),
        "kinds": ["main"],
    },
    "low-sodium": {
        "max": {"sodium": 600.0},
        "sort": ("sodium", "asc"),
        "kinds": ["main"],
    },
    "low-carb": {
        "max": {"carbohydrate": 30.0},
        "sort": ("carbohydrate", "asc"),
        "kinds": ["main"],
    },
    # 减脂综合：热量封顶后再看蛋白质密度，避免选到"低卡但全是糖"的东西
    "light": {
        "max": {"energyKcal": 500.0},
        "sort": ("protein_per_100kcal", "desc"),
        "kinds": ["main"],
    },
    # 想吃点什么但不设约束时，只在主食里按招牌/人气挑
    "any": {"sort": ("protein_per_100kcal", "desc"), "kinds": ["main"]},
}

# 餐品类型关键词表。匹配顺序：甜品 -> 饮品 -> 主食 -> 配餐 -> 兜底主食。
# 顺序有讲究："热朱古力"是饮品、"朱古力新地"是甜品，靠"新地"先命中来区分。
KINDS: List[Tuple[str, Tuple[str, ...]]] = [
    (
        "dessert",
        ("新地", "麦旋风", "圆筒", "拉明顿", "冰淇淋", "阿芙佳朵", "派"),
    ),
    (
        "drink",
        (
            "可乐", "雪碧", "咖啡", "美式", "拿铁", "奶铁", "玛奇朵", "卡布奇诺",
            "红茶", "伯爵", "牛奶", "豆浆", "纯悦", "果汁", "苹果汁", "柠檬",
            "怡泉", "雪冰", "燕麦奶", "奶冻", "爆珠", "朱古力", "黑巧", "阳光橙",
            "橙橙",
        ),
    ),
    ("main", ("堡", "卷", "麦满分", "套餐", "三件套", "饭", "粥", "薄饼")),
    (
        "side",
        (
            "薯条", "薯饼", "油条", "玉米杯", "苹果片", "麦乐鸡", "鸡翅", "V翅",
            "鸡排", "鸡球", "脆汁鸡", "香肠", "多笋卷", "沙拉", "叠叠卷",
        ),
    ),
]

# 全角标点 -> 半角，便于跨数据源对名
_PUNCT = str.maketrans("（）［］【】　", "()[][] ")


def normalize(name: str) -> str:
    """把餐品名归一化，用于跨数据源匹配。"""
    s = name.strip().translate(_PUNCT)
    s = re.sub(r"\s+", "", s)
    return s.lower()


def classify(name: str) -> str:
    """按名称判断餐品类型：main / side / drink / dessert。

    营养表没有分类字段，只能靠名称关键词推断；无法判断时归为 main，
    因为"选一份吃的"比"选一杯喝的"更符合默认期待。
    """
    for kind, keywords in KINDS:
        if any(kw in name for kw in keywords):
            return kind
    return "main"


def _to_number(raw: str) -> Optional[float]:
    raw = raw.strip()
    if not raw or raw.lower() in {"null", "none", "-", ""}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_nutrition(text: str) -> List[Dict[str, Any]]:
    """解析 list-nutrition-foods 的原始返回文本。

    形如：
        [160]{productName,nutritionDescription,energyKj,...}:
          猪柳麦满分,null,1288,308,16,16,24,781,213
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    header_idx = 0
    for i, ln in enumerate(lines):
        if re.match(r"^\[\d+\]\{", ln.strip()):
            header_idx = i
            break

    header = lines[header_idx].strip()
    inner = header[header.find("{") + 1 : header.rfind("}")]
    columns = [c.strip() for c in inner.split(",")] or COLUMNS

    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for ln in lines[header_idx + 1 :]:
        parts = ln.strip().split(",")
        if len(parts) < len(columns):
            continue
        # 餐品名可能自带逗号，反推：尾部固定 n-1 个字段
        name = ",".join(parts[: len(parts) - len(columns) + 1]).strip()
        values = [name] + [p.strip() for p in parts[len(parts) - len(columns) + 1 :]]

        row: Dict[str, Any] = {}
        for col, val in zip(columns, values):
            row[col] = _to_number(val) if col in NUMERIC else val
        if not row.get("productName"):
            continue
        # 官方营养表存在同名重复行，去重避免推荐列表出现重复项
        if row["productName"] in seen:
            continue
        seen.add(row["productName"])
        kcal = row.get("energyKcal") or 0.0
        protein = row.get("protein") or 0.0
        row["protein_per_100kcal"] = round(protein / kcal * 100, 1) if kcal > 0 else 0.0
        row["kind"] = classify(row["productName"])
        rows.append(row)
    return rows


def apply_goal(
    rows: Iterable[Dict[str, Any]],
    goal: Optional[str],
    overrides: Dict[str, float],
    kinds: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """按目标与显式阈值过滤并排序。kinds 显式传入时覆盖目标默认类型。"""
    spec = GOALS.get(goal or "", {})
    max_limits = dict(spec.get("max", {}))
    min_limits = dict(spec.get("min", {}))
    for key, value in overrides.items():
        if value is None:
            continue
        if key.startswith("max_"):
            max_limits[key[4:]] = value
        elif key.startswith("min_"):
            min_limits[key[4:]] = value

    allowed = set(kinds) if kinds else set(spec.get("kinds") or [])

    kept = []
    for row in rows:
        if allowed and row.get("kind") not in allowed:
            continue
        if any(
            row.get(f) is None or row[f] > limit for f, limit in max_limits.items()
        ):
            continue
        if any(
            (row.get(f) or 0.0) < limit for f, limit in min_limits.items()
        ):
            continue
        kept.append(row)

    sort_field, direction = spec.get("sort", ("energyKcal", "asc"))
    kept.sort(key=lambda r: r.get(sort_field) or 0.0, reverse=(direction == "desc"))
    return kept


def extract_menu_items(payload: Any) -> List[Dict[str, Any]]:
    """从 query-meals 的返回中抽出 [{code, name, price}]，兼容多种包装。"""
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    items: List[Dict[str, Any]] = []

    if isinstance(data, dict) and isinstance(data.get("meals"), dict):
        for code, meal in data["meals"].items():
            if isinstance(meal, dict) and meal.get("name"):
                items.append(
                    {
                        "code": str(code),
                        "name": meal["name"],
                        "price": meal.get("currentPrice"),
                    }
                )
        return items

    if isinstance(data, dict) and isinstance(data.get("items"), list):
        for it in data["items"]:
            if isinstance(it, dict) and it.get("name"):
                items.append(
                    {
                        "code": str(it.get("code", "")),
                        "name": it["name"],
                        "price": it.get("price"),
                    }
                )
        return items

    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and it.get("name"):
                items.append(
                    {
                        "code": str(it.get("code", "")),
                        "name": it["name"],
                        "price": it.get("price"),
                    }
                )
    return items


# 出现这些词说明菜单项是"组合"，营养库里的单品数值不能代表它
COMBO_HINTS = ("套餐", "三件套", "双人餐", "精选", "随心拼", "四宫格", "拼")


def match_menu(
    menu: List[Dict[str, Any]], nutrition: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """在售菜单 x 营养库 名称对齐。

    返回 (exact, alias, unmatched)：
      exact  —— 名称归一化后完全一致，营养数值可直接使用
      alias  —— 仅靠前缀/包含命中。营养库只有单品口径；若菜单项是套餐，
                真实热量还要叠加配餐与饮品，绝不能当成总数展示
      unmatched —— 营养库没有对应项，只能依据菜单标签判断
    """
    index: Dict[str, Dict[str, Any]] = {}
    for row in nutrition:
        index.setdefault(normalize(row["productName"]), row)

    exact: List[Dict[str, Any]] = []
    alias: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    for item in menu:
        key = normalize(item["name"])
        row = index.get(key)
        if row is not None:
            exact.append({**item, "nutrition": row, "combo": False})
            continue
        # 退一步：菜单名与营养名互为前缀（"雪碧" <-> "雪碧中杯"）
        fuzzy = next(
            (
                nrow
                for nkey, nrow in index.items()
                if nkey.startswith(key) or key.startswith(nkey)
            ),
            None,
        )
        if fuzzy is not None:
            alias.append(
                {
                    **item,
                    "nutrition": fuzzy,
                    "combo": any(hint in item["name"] for hint in COMBO_HINTS),
                }
            )
        else:
            unmatched.append(item)
    return exact, alias, unmatched


def _read(path: Optional[str]) -> str:
    if path and path != "-":
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return sys.stdin.read()


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def cmd_filter(args: argparse.Namespace) -> int:
    rows = parse_nutrition(_read(args.input))
    overrides = {
        "max_energyKcal": args.max_kcal,
        "max_fat": args.max_fat,
        "max_sodium": args.max_sodium,
        "max_carbohydrate": args.max_carb,
        "min_protein": args.min_protein,
    }
    kinds = [k.strip() for k in args.kind.split(",")] if args.kind else None
    picked = apply_goal(rows, args.goal, overrides, kinds)
    if args.limit:
        picked = picked[: args.limit]

    if args.json:
        print(json.dumps(picked, ensure_ascii=False, indent=2))
        return 0

    if not picked:
        print("没有餐品满足当前营养条件，建议放宽阈值或调整 --kind。", file=sys.stderr)
        return 1

    print(f"# 吃了麦？· 共 {len(rows)} 条营养数据，命中 {len(picked)} 条")
    if args.goal:
        print(f"# 目标：{args.goal}")
    print()
    print("| 餐品 | 类型 | 热量(kcal) | 蛋白(g) | 脂肪(g) | 碳水(g) | 钠(mg) | 蛋白/100kcal |")
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in picked:
        print(
            "| {name} | {kind} | {kcal} | {protein} | {fat} | {carb} | {sodium} | {density} |".format(
                name=row["productName"],
                kind=row.get("kind", "-"),
                kcal=_fmt(row.get("energyKcal")),
                protein=_fmt(row.get("protein")),
                fat=_fmt(row.get("fat")),
                carb=_fmt(row.get("carbohydrate")),
                sodium=_fmt(row.get("sodium")),
                density=_fmt(row.get("protein_per_100kcal")),
            )
        )
    return 0


def _row(item: Dict[str, Any], n: Dict[str, Any], note: str) -> str:
    return "| {code} | {name} | {price} | {nname} | {kcal} | {protein} | {fat} | {sodium} | {note} |".format(
        code=item["code"],
        name=item["name"],
        price=item.get("price") or "-",
        nname=n["productName"],
        kcal=_fmt(n.get("energyKcal")),
        protein=_fmt(n.get("protein")),
        fat=_fmt(n.get("fat")),
        sodium=_fmt(n.get("sodium")),
        note=note,
    )


_HEADER = (
    "| 编码 | 在售名称 | 价格(元) | 营养库名称 | 热量(kcal) | 蛋白(g) | 脂肪(g) | 钠(mg) | 说明 |",
    "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
)


def cmd_match(args: argparse.Namespace) -> int:
    nutrition = parse_nutrition(_read(args.nutrition))
    with open(args.menu, encoding="utf-8") as fh:
        menu = extract_menu_items(json.load(fh))

    exact, alias, unmatched = match_menu(menu, nutrition)
    if args.json:
        print(
            json.dumps(
                {"exact": exact, "alias": alias, "unmatched": unmatched},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(
        f"# 在售 {len(menu)} 项：精确对齐 {len(exact)}，仅单品口径 {len(alias)}，"
        f"未对齐 {len(unmatched)}"
    )
    print()
    print("## 精确对齐（营养数值可直接使用）")
    print()
    print(*_HEADER, sep="\n")
    for item in exact:
        print(_row(item, item["nutrition"], "-"))

    if alias:
        print()
        print("## 仅单品口径（⚠️ 带组合含义的，热量需再叠加配餐与饮品）")
        print()
        print(*_HEADER, sep="\n")
        for item in alias:
            note = "⚠️ 套餐：需叠加配餐+饮品" if item["combo"] else "单品口径"
            print(_row(item, item["nutrition"], note))

    if unmatched:
        print()
        print("## 未对齐（营养库暂无，按菜单标签自行判断）")
        for item in unmatched:
            print(f"- {item['code']} {item['name']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="吃了麦？营养解析/筛选/菜单对齐")
    sub = parser.add_subparsers(dest="command", required=True)

    # 兼容 `--goal low-cal` 与位置式 `low-cal` 两种写法
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("goal_pos", nargs="?", default=None, help=argparse.SUPPRESS)

    f = sub.add_parser("filter", parents=[shared], help="按营养目标筛选")
    f.add_argument("--goal", choices=sorted(GOALS), default=None)
    f.add_argument("-f", "--input", default=None, help="营养原始文本，默认 stdin")
    f.add_argument("--max-kcal", type=float, default=None)
    f.add_argument("--max-fat", type=float, default=None)
    f.add_argument("--max-sodium", type=float, default=None)
    f.add_argument("--max-carb", type=float, default=None)
    f.add_argument("--min-protein", type=float, default=None)
    f.add_argument(
        "--kind",
        default=None,
        help="只保留指定类型，逗号分隔：main,side,drink,dessert",
    )
    f.add_argument("--limit", type=int, default=None)
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_filter)

    m = sub.add_parser("match", help="在售菜单与营养库对齐")
    m.add_argument("--menu", required=True, help="query-meals 返回的 JSON 文件")
    m.add_argument("--nutrition", default=None, help="营养原始文本，默认 stdin")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_match)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "goal", None) is None and getattr(args, "goal_pos", None):
        args.goal = args.goal_pos
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
