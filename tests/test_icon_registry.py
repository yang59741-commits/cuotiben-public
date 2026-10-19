import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "cuoti_app" / "static"
JS = STATIC / "js"
TEMPLATES = ROOT / "cuoti_app" / "templates"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def registered_icons() -> set[str]:
    source = read(JS / "icons.js")
    body = source[source.index("const ICON_PATHS=Object.freeze({") : source.index("});")]
    return set(re.findall(r"^\s*([A-Za-z][A-Za-z0-9]*):'", body, re.MULTILINE))


def test_every_literal_icon_name_is_registered():
    icons = registered_icons()
    used: dict[str, set[str]] = {}
    for path in sorted(JS.rglob("*.js")):
        if "vendor" in path.relative_to(JS).parts:
            continue
        source = read(path)
        for name in re.findall(r"\bicon\('([A-Za-z0-9]+)'", source):
            used.setdefault(name, set()).add(str(path.relative_to(ROOT)))
        for name in re.findall(r"\bicon:'([A-Za-z0-9]+)'", source):
            used.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    for path in sorted(TEMPLATES.rglob("*.html")):
        for name in re.findall(r'data-icon="([A-Za-z0-9]+)"', read(path)):
            used.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    for name in re.findall(r"statsEmptyHTML\('[^']*','([A-Za-z0-9]+)'", stats_source()):
        used.setdefault(name, set()).add("cuoti_app/static/js/stats/render.js")
    missing = {name: sorted(files) for name, files in used.items() if name not in icons}
    assert not missing, f"icons used but not defined in icons.js: {missing}"
    for name in ("arrowRight", "circleAlert", "shieldCheck"):
        assert name in used


def test_unknown_icon_warns_once_and_keeps_visible_fallback():
    script = f"""
const fs=require('fs');const vm=require('vm');
const warnings=[];const context={{console:{{warn:m=>warnings.push(m)}},window:{{}},document:{{addEventListener(){{}}}}}};
vm.createContext(context);vm.runInContext(fs.readFileSync({str(JS / 'icons.js')!r},'utf8'),context);
const a=context.window.icon('notAnIcon');context.window.icon('notAnIcon');const b=context.window.icon('toString');
if(!a.includes('<circle cx="12" cy="12" r="9"/>'))throw new Error('fallback missing');
if(warnings.length!==2||!warnings[0].includes('notAnIcon'))throw new Error(JSON.stringify(warnings));
if(!context.window.icon('arrowRight').includes('M5 12h14'))throw new Error('arrowRight missing');
"""
    subprocess.run(["node", "-e", script], check=True)


def stats_source() -> str:
    return "\n".join(read(path) for path in [JS / "stats.js", *sorted((JS / "stats").glob("*.js"))])


def test_statistics_ledger_and_review_journey_use_registered_icons():
    stats = stats_source()
    css = read(STATIC / "css" / "pages" / "stats.css")
    assert "${icon('rotateCcw')}本期 <b>${count(cycle.reset_mistakes)}</b> 道答错后回到第1轮" in stats
    assert "${icon('repeat')}开始间隔复习" in stats
    assert "${icon(table?'chart':'table')}" in stats
    assert "table:'<rect" in read(JS / "icons.js")
    assert "答错就回到" not in stats
    assert ".stats-pipe-foot .icon{display:inline-block;width:14px;height:14px" in css


def test_statistics_headers_empty_states_and_insights_use_matching_icons():
    template = read(TEMPLATES / "partials" / "pages" / "stats.html")
    stats = stats_source()
    render = read(JS / "ui" / "render.js")
    assert '<span data-icon="calendar"></span>学习节奏' in template
    assert '<span data-icon="barsHorizontal"></span>章节进度' in template
    assert '<span data-icon="pie"></span>错因' in template
    assert '<span data-icon="lightbulb"></span>需要留意' in template
    assert 'data-icon="sparkles"' not in template
    empty_icons = set(re.findall(r"statsEmptyHTML\('[^']*','([A-Za-z0-9]+)'", stats))
    assert empty_icons == {"barsHorizontal", "pie", "calendar", "examPaper", "history", "lightbulb"}
    allowed = render[render.index("EMPTY_STATE_ICON_NAMES=new Set([") :]
    allowed = allowed[: allowed.index("]);")]
    for name in empty_icons | {"chart"}:
        assert f"'{name}'" in allowed
    assert "<article class=\"insight triggered\">${icon('circleAlert')}" in stats
    assert "calm(normal,'normal','circleCheck','状态正常'+againNote)" in stats
    assert "calm(insufficient,'insufficient','circleDashed','样本不足，暂不判断')" in stats


def test_statistics_icons_follow_three_size_tokens():
    tokens = read(STATIC / "css" / "tokens.css")
    css = read(STATIC / "css" / "pages" / "stats.css")
    assert "--icon-sm:16px;--icon-md:18px;--icon-lg:20px;" in tokens
    assert ".chart-card-header h4 .icon{width:var(--icon-md);height:var(--icon-md)" in css
    assert "#p-stats .empty-state-icon .icon{width:var(--icon-lg);height:var(--icon-lg)}" in css
    assert "#p-stats .btn .icon{width:var(--icon-sm);height:var(--icon-sm)}" in css
