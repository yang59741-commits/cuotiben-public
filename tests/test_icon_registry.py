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
    stats = read(JS / "stats.js")
    for name in re.findall(r"statsEmptyHTML\('[^']*',(?:true|false),'([A-Za-z0-9]+)'\)", stats):
        used.setdefault(name, set()).add("cuoti_app/static/js/stats.js")
    for name in re.findall(r"^\s*[a-z]+:\['([A-Za-z0-9]+)','", stats, re.MULTILINE):
        used.setdefault(name, set()).add("cuoti_app/static/js/stats.js")
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


def test_statistics_metric_cards_use_semantic_tones_and_icons():
    stats = read(JS / "stats.js")
    css = read(STATIC / "css" / "pages" / "stats.css")
    metrics = stats[stats.index("const metrics=[") : stats.index("const reviewScope=")]
    assert "tone:'good',visual:statsRingHTML(s.current_mastery_rate),label:'当前掌握率'" in metrics
    assert "tone:'accent',visual:icon('refresh'),label:'今日间隔复习'" in metrics
    assert "tone:'warn',visual:icon('rotateCcw'),label:'再次答错率'" in metrics
    assert "circleHelp" not in metrics
    assert 'class="stat stat-action stat-tone-${item.tone}" data-action="open-spaced-review"' in stats
    assert "<span class=\"stat-go\" aria-hidden=\"true\">${icon('arrowRight')}</span>" in stats
    assert "function statsRingHTML(rate)" in stats
    assert "if(!Number.isFinite(value))return icon('check')" in stats
    assert ".stat.stat-tone-good{color:var(--green)}" in css
    assert ".stat.stat-tone-accent{color:var(--accent)}" in css
    assert ".stat.stat-tone-warn{color:var(--orange)}" in css


def test_statistics_headers_empty_states_and_insights_use_matching_icons():
    template = read(TEMPLATES / "partials" / "pages" / "stats.html")
    stats = read(JS / "stats.js")
    render = read(JS / "ui" / "render.js")
    assert '<span data-icon="barsHorizontal"></span>章节掌握分布' in template
    assert '<span data-icon="pie"></span>错因构成' in template
    assert '<span data-icon="lightbulb"></span>学习洞察与提醒' in template
    assert 'data-icon="sparkles"' not in template
    empty_icons = set(re.findall(r"statsEmptyHTML\('[^']*',(?:true|false),'([A-Za-z0-9]+)'\)", stats))
    assert empty_icons == {"barsHorizontal", "pie", "calendar", "fileText"}
    allowed = render[render.index("EMPTY_STATE_ICON_NAMES=new Set([") :]
    allowed = allowed[: allowed.index("]);")]
    for name in empty_icons | {"chart"}:
        assert f"'{name}'" in allowed
    assert "triggered:['circleAlert','需要关注']" in stats
    assert "normal:['circleCheck','状态正常']" in stats
    assert "insufficient:['circleDashed','样本不足']" in stats
    assert "${statsInsightStatusHTML(item.status)}" in stats
    assert "${icon('bell')}${safeInteger(reminder.due_count)} 道" in stats


def test_statistics_icons_follow_three_size_tokens():
    tokens = read(STATIC / "css" / "tokens.css")
    css = read(STATIC / "css" / "pages" / "stats.css")
    assert "--icon-sm:16px;--icon-md:18px;--icon-lg:20px;" in tokens
    assert ".chart-card-header h4 .icon{width:var(--icon-md);height:var(--icon-md)" in css
    assert "#stat-box .stat-icon>.icon{width:var(--icon-lg);height:var(--icon-lg)}" in css
    assert "#p-stats .empty-state-icon .icon{width:var(--icon-lg);height:var(--icon-lg)}" in css
    assert "#p-stats .insight-status .icon{width:var(--icon-sm);height:var(--icon-sm)}" in css
    assert "#p-stats .btn .icon{width:var(--icon-sm);height:var(--icon-sm)}" in css
