"""G27：抓取侧隐藏内容剥离 —— 白字/隐藏注入不得出现在渲染结果里。

**修前会怎样**：``html_to_markdown`` 只扔 ``script/style/nav/header/footer/noscript``。
攻击者把诱导句放进 ``<div style="display:none">`` / ``<span hidden>`` /
``<p aria-hidden="true">`` / ``<div style="color:#fff;background:#fff;opacity:0">``，
人眼（以及用户的浏览器截图）里什么都看不到，但 ``get_text`` 照样把它渲染进 markdown ——
模型收到的是"网页正文里有这么一句"，与页面上看得见的内容完全同级。

**为什么这么改**：判定只在**抓取侧**做，抽成纯函数以便单测（不做 CSS 引擎，
只覆盖"声明式隐藏"这一档：属性、ARIA、内联样式里的常见隐藏声明）。
既有 ``script/style/nav/header/footer/noscript`` 剥离保持不变，只做加法、不回退。
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from app.tools.web import html_to_markdown, is_hidden_element, is_hidden_style

# ── ① 纯函数：内联样式判定 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "style",
    [
        "display:none",
        "display: none !important",
        "DISPLAY:NONE",
        "visibility:hidden",
        "visibility: hidden !important",
        "visibility:collapse",
        "opacity:0",
        "opacity:.0",
        "opacity:0.0",
        "opacity:0%",
        "height:0",
        "height:0px",
        "max-height:0",
        "font-size:0",
        "font-size:0px",
        # 屏幕阅读器/人眼都看不到，但 get_text 拿得到 —— 正是白字注入的常用档
        "text-indent:-9999px",
        "position:absolute;left:-9999px",
        "position:absolute;top:-10000px",
        "position:fixed;margin-left:-9999em",
        "transform:scale(0)",
        "clip:rect(0,0,0,0)",
        "clip-path:inset(50%)",
        "overflow:hidden;height:0",
        "color:#fff;background-color:#fff;opacity:0",
    ],
)
def test_hidden_style_is_detected(style):
    assert is_hidden_style(style) is True, f"应判定为隐藏：{style}"


@pytest.mark.parametrize(
    "style",
    [
        "",
        None,
        "color:#333",
        "display:block",
        "display:flex",
        "visibility:visible",
        "opacity:0.5",
        "opacity:1",
        "height:12px",
        "font-size:14px",
        "left:8px",
        "position:relative;top:-4px",
        "text-indent:2em",
        "transform:scale(1.02)",
        "clip-path:circle(50%)",
        # 值里带 "none" 但不是 display 的隐藏声明（宽松但不能宽松到误删正文）
        "text-decoration:none",
        "background:none",
        "border-style:none",
    ],
)
def test_visible_style_is_not_flagged(style):
    assert is_hidden_style(style) is False, f"不应判定为隐藏：{style!r}"


def test_hidden_style_is_explainable():
    """判定要能说出**为什么**隐藏（排查误删正文时唯一可用的线索）。"""
    from app.tools.web import hidden_style_reason

    assert hidden_style_reason("display:none") == "display:none"
    assert hidden_style_reason("color:red; visibility: hidden !important") == (
        "visibility:hidden"
    )
    assert hidden_style_reason("color:red") == ""


# ── ② 纯函数：元素级判定（属性 / ARIA / input[type=hidden]） ─────────────


def _tag(html: str):
    return BeautifulSoup(html, "html.parser").find(True)


@pytest.mark.parametrize(
    "html",
    [
        "<div hidden>注入</div>",
        '<div hidden="">注入</div>',
        '<div aria-hidden="true">注入</div>',
        '<div aria-hidden="TRUE">注入</div>',
        '<input type="hidden" name="x" value="注入">',
        '<div style="display:none">注入</div>',
        "<iframe src='x'>注入</iframe>",
        "<template>注入</template>",
        "<object data='x'>注入</object>",
        "<embed src='x'>",
        "<noscript>注入</noscript>",
    ],
)
def test_hidden_element_is_detected(html):
    assert is_hidden_element(_tag(html)) is True


@pytest.mark.parametrize(
    "html",
    [
        "<div>正文</div>",
        '<div aria-hidden="false">正文</div>',
        '<input type="text" value="正文">',
        '<div style="color:#333">正文</div>',
        '<div class="sr-only-nope">正文</div>',
    ],
)
def test_visible_element_is_not_flagged(html):
    assert is_hidden_element(_tag(html)) is False


# ── ③ 端到端：渲染结果里看不到隐藏文本 ───────────────────────────────────


def test_hidden_attributes_are_stripped_from_markdown():
    """属性式隐藏：`hidden` / `aria-hidden` / `input[type=hidden]`。"""
    html = """
    <html><body>
      <p>看得见的正文</p>
      <div hidden>HIDDEN_ATTR 注入</div>
      <div aria-hidden="true">ARIA_HIDDEN 注入</div>
      <form><input type="hidden" name="x" value="INPUT_HIDDEN 注入"></form>
    </body></html>
    """
    md = html_to_markdown(html)

    assert "看得见的正文" in md
    assert "HIDDEN_ATTR" not in md
    assert "ARIA_HIDDEN" not in md
    assert "INPUT_HIDDEN" not in md


def test_inline_style_hidden_is_stripped_from_markdown():
    """内联样式式隐藏：白字注入最常见的落地方式（人眼看空、机器照读）。"""
    html = """
    <html><body>
      <p>看得见的正文</p>
      <div style="display:none">DISPLAY_NONE 注入</div>
      <span style="visibility:hidden">VISIBILITY 注入</span>
      <span style="opacity:0">OPACITY 注入</span>
      <div style="height:0;overflow:hidden">HEIGHT_ZERO 注入</div>
      <div style="color:#fff;background:#fff;font-size:0">FONT_ZERO 注入</div>
      <div style="position:absolute;left:-9999px">OFFSCREEN 注入</div>
    </body></html>
    """
    md = html_to_markdown(html)

    assert "看得见的正文" in md
    for leaked in (
        "DISPLAY_NONE", "VISIBILITY", "OPACITY",
        "HEIGHT_ZERO", "FONT_ZERO", "OFFSCREEN",
    ):
        assert leaked not in md, f"隐藏文本泄漏：{leaked}"


def test_hidden_container_strips_whole_subtree():
    """隐藏的是容器时，整棵子树一起丢（只丢容器文字挡不住子节点里的注入）。"""
    html = """
    <html><body>
      <p>看得见的正文</p>
      <div hidden><p>SUB_A 注入</p><span>SUB_B 注入</span><b>SUB_C 注入</b></div>
    </body></html>
    """
    md = html_to_markdown(html)

    assert "看得见的正文" in md
    for leaked in ("SUB_A", "SUB_B", "SUB_C"):
        assert leaked not in md


def test_existing_strips_do_not_regress():
    """既有剥离不回退：脚本/样式/导航/页眉页脚/noscript/iframe/object/embed/template。"""
    html = """
    <html><body>
      <header>HEADER 文本</header>
      <nav>NAV 文本</nav>
      <script>var x = "SCRIPT 文本";</script>
      <style>.a{content:"STYLE 文本"}</style>
      <noscript>NOSCRIPT 文本</noscript>
      <template>TEMPLATE 文本</template>
      <iframe src="https://e.com">IFRAME 文本</iframe>
      <object data="x">OBJECT 文本</object>
      <embed src="x" title="EMBED 文本">
      <footer>FOOTER 文本</footer>
      <p>看得见的正文</p>
    </body></html>
    """
    md = html_to_markdown(html)

    assert "看得见的正文" in md
    for leaked in (
        "HEADER 文本", "NAV 文本", "SCRIPT 文本", "STYLE 文本", "NOSCRIPT 文本",
        "TEMPLATE 文本", "IFRAME 文本", "OBJECT 文本", "EMBED 文本", "FOOTER 文本",
    ):
        assert leaked not in md, f"既有剥离回退：{leaked}"


def test_visible_markdown_structure_survives():
    """剥离只做减法：标题/列表/链接/表格仍按原样渲染。"""
    html = """
    <html><body>
      <h2>标题</h2>
      <p>段落 <a href="https://x.com">X</a></p>
      <ul><li>一</li><li>二</li></ul>
      <table><tr><td>c1</td><td>c2</td></tr></table>
      <div style="color:#333">普通 div</div>
    </body></html>
    """
    md = html_to_markdown(html)

    assert "## 标题" in md
    assert "[X](https://x.com)" in md
    assert "- 一" in md and "- 二" in md
    assert "| c1 | c2 |" in md
    assert "普通 div" in md
