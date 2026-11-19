"""Page Schema → 静态前端代码（P4-T03 / M4a-W42）。

产物是三个文件的编译结果（`index.html` / `app.js` / `theme.css`），随包冻结，
运行期不再编译。三条硬性要求：

1. **确定性**：同一 Schema 编译两次字节一致（json 排序 + 固定模板 + 无时间戳），
   否则包的 `manifest_hash` 每次发布都会变；
2. **无可执行片段**：不生成 `eval`、不生成内联事件属性、不拼接未转义插值，
   文本一律走 `textContent`；编译完再断言一次（校验器是第一道闸，这里是第二道）；
3. **零 Node 依赖**：Python 直出字符串，产物进 `ui/compiled/<page>/` 由 FastAPI 托管。
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from ao.pages import schema as page_schema

CSP = (
    "default-src 'none'; style-src 'self'; script-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'"
)

#: 生成物里禁止出现的可执行片段（第二道闸）
_FORBIDDEN_JS = ("eval(", "new Function(", "document.write(")
_INLINE_EVENT_RE = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)

_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>{title}</title>
<link rel="stylesheet" href="theme.css">
</head>
<body>
<div id="app" data-page="{page_id}"></div>
<script src="app.js" defer></script>
</body>
</html>
"""

_RUNTIME = r"""
"use strict";
(function () {
  var PAGE = __PAGE__;
  var CTX = window.__AO_PAGE__ || {};
  var apiBase = CTX.apiBase || "/api/page";
  var state = { data: {}, filters: {}, selection: null, error: "" };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null && text !== "") {
      node.textContent = String(text);
    }
    return node;
  }

  function readPath(base, parts) {
    var current = base;
    for (var i = 0; i < parts.length; i += 1) {
      if (current === null || current === undefined) { return undefined; }
      current = current[parts[i]];
    }
    return current;
  }

  function expr(value) {
    if (typeof value !== "string" || value.charAt(0) !== "$") { return value; }
    var parts = value.slice(1).split(".");
    var root = parts.shift();
    var base = {};
    if (root === "filters") { base = state.filters; }
    if (root === "selection") { base = state.selection || {}; }
    if (root === "workflow") { base = PAGE.meta || {}; }
    if (root === "user") { base = CTX.user || {}; }
    return readPath(base, parts);
  }

  function resolveParams(params) {
    var out = {};
    var source = params || {};
    Object.keys(source).forEach(function (key) {
      var value = expr(source[key]);
      if (value !== undefined && value !== null && value !== "") { out[key] = value; }
    });
    return out;
  }

  function fetchSource(name) {
    var spec = (PAGE.data || {})[name];
    if (!spec) { return Promise.resolve(); }
    var query = new URLSearchParams(resolveParams(spec.params));
    query.set("limit", String(spec.limit || 200));
    var url = apiBase + "/" + encodeURIComponent(spec.source) + "?" + query.toString();
    return fetch(url, { credentials: "omit" })
      .then(function (resp) {
        if (!resp.ok) { throw new Error("数据源 " + spec.source + " 返回 " + resp.status); }
        return resp.json();
      })
      .then(function (body) { state.data[name] = body.rows || []; });
  }

  function fetchAll() {
    var names = Object.keys(PAGE.data || {});
    return Promise.all(names.map(fetchSource));
  }

  function runAction(action, payload) {
    if (!action) { return; }
    if (action.confirm && !window.confirm("确认执行该操作？")) { return; }
    var body = {
      action: action.action,
      node: action.node || null,
      run: action.run ? expr(action.run) : null,
      payload: payload || action.payload || {}
    };
    fetch(apiBase + "/action", {
      method: "POST",
      credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (resp) {
        if (!resp.ok) { throw new Error("动作 " + body.action + " 返回 " + resp.status); }
        return resp.json();
      })
      .then(function () { return fetchAll(); })
      .then(render)
      .catch(function (err) { state.error = String(err.message || err); render(); });
  }

  function buildFilter(block) {
    var form = el("form", "filter");
    (block.fields || []).forEach(function (field) {
      var wrap = el("label", "filter-item");
      wrap.appendChild(el("span", "filter-label", field.label));
      var input;
      if (field.kind === "select") {
        input = el("select", "filter-input");
        (field.options || []).forEach(function (opt) {
          var option = el("option", null, opt.label);
          option.value = String(opt.value);
          input.appendChild(option);
        });
      } else {
        input = el("input", "filter-input");
        input.type = field.kind === "number" ? "number" : field.kind === "date" ? "date" : "text";
      }
      input.setAttribute("name", field.name);
      wrap.appendChild(input);
      form.appendChild(wrap);
    });
    form.appendChild(el("button", "btn", block.title || "筛选"));
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var controls = form.querySelectorAll("[name]");
      state.filters = {};
      Array.prototype.forEach.call(controls, function (control) {
        if (control.value) { state.filters[control.name] = control.value; }
      });
      fetchAll().then(render);
    });
    return form;
  }

  function cellText(row, column) {
    var value = row[column.field];
    return value === undefined || value === null ? "" : value;
  }

  function buildTable(block) {
    var wrap = el("div", "table-wrap");
    var table = el("table", "table");
    var head = el("thead");
    var headRow = el("tr");
    (block.columns || []).forEach(function (column) {
      var th = el("th", null, column.label);
      if (column.width) { th.style.width = column.width + "px"; }
      headRow.appendChild(th);
    });
    head.appendChild(headRow);
    table.appendChild(head);
    var body = el("tbody");
    var rows = state.data[block.source] || [];
    rows.forEach(function (row) {
      var tr = el("tr", null);
      (block.columns || []).forEach(function (column) {
        tr.appendChild(el("td", null, cellText(row, column)));
      });
      if (block.onRowClick) {
        tr.className = "row-clickable";
        tr.addEventListener("click", function () {
          state.selection = row;
          var target = (PAGE.data || {})[block.onRowClick.source];
          if (!target) { render(); return; }
          var params = target.params || {};
          params[block.onRowClick.param || "id"] = row.id;
          fetchSource(block.onRowClick.source).then(render);
        });
      }
      body.appendChild(tr);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
  }

  function buildDetail(block) {
    var rows = state.data[block.source] || [];
    var row = rows[0] || state.selection || {};
    var box = el("section", "detail");
    if (block.title) { box.appendChild(el("h2", "block-title", block.title)); }
    var list = el("dl", "detail-list");
    (block.fields || []).forEach(function (field) {
      list.appendChild(el("dt", null, field.label));
      var value = row[field.name];
      list.appendChild(el("dd", null, value === undefined || value === null ? "-" : value));
    });
    box.appendChild(list);
    return box;
  }

  function buildTimeline(block) {
    var box = el("section", "timeline");
    if (block.title) { box.appendChild(el("h2", "block-title", block.title)); }
    var items = state.data[block.source] || [];
    items.forEach(function (row) {
      var item = el("article", "timeline-item");
      item.appendChild(el("div", "timeline-time", row[block.timeField]));
      item.appendChild(el("div", "timeline-title", row[block.titleField]));
      if (block.detailField && row[block.detailField]) {
        item.appendChild(el("div", "timeline-detail", row[block.detailField]));
      }
      box.appendChild(item);
    });
    return box;
  }

  function buildForm(block) {
    var form = el("form", "form");
    if (block.title) { form.appendChild(el("h2", "block-title", block.title)); }
    (block.fields || []).forEach(function (field) {
      var wrap = el("label", "form-item");
      wrap.appendChild(el("span", "form-label", field.label + (field.required ? " *" : "")));
      var input;
      if (field.kind === "select") {
        input = el("select", "form-input");
        (field.options || []).forEach(function (opt) {
          var option = el("option", null, opt.label);
          option.value = String(opt.value);
          input.appendChild(option);
        });
      } else if (field.kind === "boolean") {
        input = el("input", "form-input");
        input.type = "checkbox";
      } else {
        input = el("input", "form-input");
        input.type = field.kind === "number" ? "number" : field.kind === "date" ? "date" : "text";
      }
      input.setAttribute("name", field.name);
      if (field.required) { input.required = true; }
      wrap.appendChild(input);
      form.appendChild(wrap);
    });
    var submit = el("button", "btn btn-primary", block.title || "提交");
    submit.type = "submit";
    form.appendChild(submit);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var payload = {};
      var controls = form.querySelectorAll("[name]");
      Array.prototype.forEach.call(controls, function (control) {
        if (control.type === "checkbox") {
          payload[control.name] = control.checked;
        } else if (control.value !== "") {
          payload[control.name] = control.value;
        }
      });
      runAction(block.submit, payload);
    });
    return form;
  }

  function buildList(block) {
    var box = el("ul", "list");
    var items = state.data[block.source] || [];
    items.forEach(function (row) {
      var item = el("li", "list-item");
      item.appendChild(el("div", "list-title", row[block.itemTitle]));
      if (block.itemSubtitle) {
        item.appendChild(el("div", "list-subtitle", row[block.itemSubtitle]));
      }
      box.appendChild(item);
    });
    return box;
  }

  function buildCard(block) {
    var box = el("div", "card-grid");
    var items = state.data[block.source] || [];
    var fields = block.fields || [];
    items.forEach(function (row) {
      var card = el("article", "card");
      card.appendChild(el("h3", "card-title", row[block.titleField] || ""));
      var list = el("dl", "card-fields");
      fields.forEach(function (field) {
        list.appendChild(el("dt", null, field.label));
        list.appendChild(el("dd", null, row[field.name]));
      });
      card.appendChild(list);
      box.appendChild(card);
    });
    return box;
  }

  function buildCalendar(block) {
    var box = el("div", "calendar");
    var items = state.data[block.source] || [];
    items.forEach(function (row) {
      var rowEl = el("div", "calendar-row");
      var range = row[block.startField] + " ~ " + row[block.endField];
      rowEl.appendChild(el("span", "calendar-range", range));
      rowEl.appendChild(el("span", "calendar-title", row[block.titleField]));
      box.appendChild(rowEl);
    });
    return box;
  }

  function build(block) {
    if (!block) { return null; }
    if (block.visibleIf && !expr(block.visibleIf)) { return null; }
    var node;
    if (block.type === "text") { node = el("p", "text", block.text); }
    else if (block.type === "table") { node = buildTable(block); }
    else if (block.type === "filter") { node = buildFilter(block); }
    else if (block.type === "detail") { node = buildDetail(block); }
    else if (block.type === "timeline") { node = buildTimeline(block); }
    else if (block.type === "form") { node = buildForm(block); }
    else if (block.type === "list") { node = buildList(block); }
    else if (block.type === "card") { node = buildCard(block); }
    else if (block.type === "calendar") { node = buildCalendar(block); }
    else if (block.type === "image") {
      node = el("img", "image");
      node.setAttribute("src", block.src);
      node.setAttribute("alt", block.alt || "");
    } else if (block.type === "button") {
      node = el("button", "btn", block.label);
      node.type = "button";
      node.addEventListener("click", function () { runAction(block.action); });
    } else if (block.type === "tabs") {
      node = el("div", "tabs");
      (block.tabs || []).forEach(function (tab) {
        var section = el("section", "tab");
        section.appendChild(el("h2", "block-title", tab.label));
        (tab.blocks || []).forEach(function (inner) {
          var child = build(inner);
          if (child) { section.appendChild(child); }
        });
        node.appendChild(section);
      });
    } else {
      node = el("div", "unsupported", "不支持的组件: " + block.type);
    }
    if (node && block.title && block.type !== "form" && block.type !== "detail") {
      var wrap = el("section", "block block-" + block.type);
      wrap.appendChild(el("h2", "block-title", block.title));
      wrap.appendChild(node);
      return wrap;
    }
    return node;
  }

  function render() {
    var root = document.getElementById("app");
    if (!root) { return; }
    root.textContent = "";
    var layout = el("div", "page layout-" + (PAGE.layout || "stack"));
    if (PAGE.title) { layout.appendChild(el("h1", "page-title", PAGE.title)); }
    if (state.error) { layout.appendChild(el("div", "page-error", state.error)); }
    (PAGE.blocks || []).forEach(function (block) {
      var node = build(block);
      if (node) { layout.appendChild(node); }
    });
    root.appendChild(layout);
  }

  fetchAll()
    .then(render)
    .catch(function (err) {
      state.error = String(err.message || err);
      render();
    });
})();
"""

_CSS_TEMPLATE = """/* 由 Page Schema 编译生成，请勿手改 */
:root {{
  --ao-primary: {primary};
  --ao-bg: {background};
  --ao-text: {text};
  --ao-border: {border};
  --ao-gap: {gap};
}}

* {{ box-sizing: border-box; }}

body {{
  margin: 0;
  padding: 16px;
  background: var(--ao-bg);
  color: var(--ao-text);
  font: 14px/1.5 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}}

.page {{ display: flex; flex-direction: column; gap: var(--ao-gap); }}
.page.layout-sidebar {{ display: grid; grid-template-columns: 220px 1fr; align-items: start; }}
.page-title {{ margin: 0; font-size: 20px; }}
.page-error {{
  padding: 8px 12px;
  border: 1px solid #d33;
  border-radius: 6px;
  color: #d33;
  background: #fff5f5;
}}

.block-title {{ margin: 0 0 8px; font-size: 15px; font-weight: 600; }}
.text {{ margin: 0; white-space: pre-wrap; }}

.btn {{
  padding: 6px 12px;
  border: 1px solid var(--ao-border);
  border-radius: 6px;
  background: #fff;
  color: var(--ao-text);
  cursor: pointer;
}}
.btn-primary {{ background: var(--ao-primary); border-color: var(--ao-primary); color: #fff; }}

.table-wrap {{ overflow-x: auto; }}
.table {{ width: 100%; border-collapse: collapse; background: #fff; }}
.table th, .table td {{
  padding: 8px 10px;
  border-bottom: 1px solid var(--ao-border);
  text-align: left;
}}
.table th {{ font-weight: 600; background: #fafafa; }}
.row-clickable {{ cursor: pointer; }}
.row-clickable:hover {{ background: #f5f7fa; }}

.filter, .form {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end; }}
.filter-item, .form-item {{ display: flex; flex-direction: column; gap: 4px; min-width: 160px; }}
.filter-label, .form-label {{ font-size: 12px; color: #666; }}
.filter-input, .form-input {{
  padding: 6px 8px;
  border: 1px solid var(--ao-border);
  border-radius: 6px;
  font: inherit;
}}

.detail-list, .card-fields {{
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 6px 12px;
  margin: 0;
}}
.detail-list dt, .card-fields dt {{ color: #666; }}
.detail-list dd, .card-fields dd {{ margin: 0; }}

.timeline {{ display: flex; flex-direction: column; gap: 8px; }}
.timeline-item {{ padding-left: 12px; border-left: 2px solid var(--ao-border); }}
.timeline-time {{ font-size: 12px; color: #888; }}
.timeline-title {{ font-weight: 600; }}
.timeline-detail {{ color: #555; }}

.list {{
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}}
.list-item {{
  padding: 8px 10px;
  background: #fff;
  border: 1px solid var(--ao-border);
  border-radius: 6px;
}}
.list-title {{ font-weight: 600; }}
.list-subtitle {{ font-size: 12px; color: #666; }}

.card-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--ao-gap);
}}
.card {{ padding: 12px; background: #fff; border: 1px solid var(--ao-border); border-radius: 8px; }}
.card-title {{ margin: 0 0 8px; font-size: 14px; }}

.calendar {{ display: flex; flex-direction: column; gap: 6px; }}
.calendar-row {{
  display: flex;
  gap: 12px;
  padding: 6px 0;
  border-bottom: 1px solid var(--ao-border);
}}
.calendar-range {{ color: #666; min-width: 180px; }}

.tabs {{ display: flex; flex-direction: column; gap: var(--ao-gap); }}
.tab {{ padding: 12px; border: 1px solid var(--ao-border); border-radius: 8px; background: #fff; }}
.image {{ max-width: 100%; }}
.unsupported {{ padding: 8px 12px; border: 1px dashed var(--ao-border); color: #888; }}
"""

_DEFAULT_THEME = {
    "primary": "#2f6fed",
    "background": "#f7f8fa",
    "text": "#1f2329",
    "border": "#dee2e8",
}


def compile_page(raw: dict[str, Any]) -> dict[str, str]:
    """把一份 Schema 编译成 `{文件名: 内容}`；不落盘，便于预览与测试。

    校验失败抛 `PageSchemaError`（带全部 E_* 问题），调用方不需要再自己做类型判断。
    """
    schema = page_schema.load(raw)
    page_id = schema["id"]
    title = schema["title"]
    page_json = json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2)

    html_text = _HTML_TEMPLATE.format(
        csp=CSP,
        title=html.escape(title, quote=True),
        page_id=html.escape(page_id, quote=True),
    )
    js_text = _RUNTIME.replace("__PAGE__", _indent_block(page_json))
    css_text = _CSS_TEMPLATE.format(**_theme_values(schema.get("theme") or {}))

    _assert_safe(html_text, js_text)
    return {"index.html": html_text, "app.js": js_text, "theme.css": css_text}


def write_page(raw: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    """编译并写盘；返回各文件路径，调用方负责把目录放进包里。"""
    files = compile_page(raw)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, content in files.items():
        path = out_dir / name
        # newline="\n" 固定换行，避免 Windows 上编译出不同字节
        path.write_text(content, encoding="utf-8", newline="\n")
        written[name] = path
    assets = out_dir / "assets"
    assets.mkdir(exist_ok=True)
    return written


def _theme_values(theme: dict[str, str]) -> dict[str, str]:
    merged = {**_DEFAULT_THEME, **theme}
    return {
        "primary": merged["primary"],
        "background": merged["background"],
        "text": merged["text"],
        "border": merged["border"],
        "gap": "12px",
    }


def _indent_block(text: str) -> str:
    """把 JSON 缩进到 IIFE 内部，保持产物可读且字节稳定。"""
    return "\n".join("  " + line for line in text.splitlines())


def _assert_safe(html_text: str, js_text: str) -> None:
    for token in _FORBIDDEN_JS:
        if token in js_text:
            raise AssertionError(f"编译产物含禁止片段: {token}")
    for name, text in (("index.html", html_text), ("app.js", js_text)):
        if _INLINE_EVENT_RE.search(text):
            raise AssertionError(f"{name} 出现内联事件属性")
        if "javascript:" in text:
            raise AssertionError(f"{name} 出现 javascript: 链接")
