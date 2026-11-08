#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""控制流检查：找出「只在某些分支里被赋值，却在分支外被读取」的变量。

补 pyflakes 的缺口。实测的教训：
  · 第一类（变量名写错，例如 extra / concede_note 混用）—— pyflakes 能抓
  · 第二类（只在 if / try 里赋值，分支外读）—— pyflakes **抓不到**
    这种代码平时不炸，只有走到那个分支才 UnboundLocalError，
    而我的自检当时是"手写复刻"那段逻辑，复刻对了、真代码错了，所以没拦住。

所以这里对源码做一次**确定赋值分析**（definite-assignment）：
顺着函数体走，维护"到这里一定已经赋值过"的集合；读到不在集合里、
又不是参数/全局/内置的名字就报出来。

跑法：
  python unbound_check.py            # 检查本目录下所有 .py
  python unbound_check.py brain.py
退出码 0 = 没发现问题。
"""
from __future__ import annotations

import ast
import builtins
import pathlib
import sys

# Windows 控制台默认是 GBK，打印 ✅ 会 UnicodeEncodeError 直接崩掉检查器本身。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "self", "cls"}


def _terminates(body: list[ast.stmt]) -> bool:
    """这段代码是不是"必然不会往下走"（return / raise / continue / break）。

    这条规则很关键：真实代码里大量用
        try:
            x = 可能有异常的取值()
        except Exception:
            return / continue      ← 出错就不往下走了
        用 x                        ← 所以这里 x 一定已经绑定
    不建模"终止"的话，这类正确写法会被全部误报（实测 39 条误报全来自这里）。
    """
    for st in body:
        if isinstance(st, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            return True
        if isinstance(st, ast.If):
            if st.orelse and _terminates(st.body) and _terminates(st.orelse):
                return True
        if isinstance(st, ast.Try):
            if _terminates(st.body):
                return True
            if st.handlers and all(_terminates(h.body) for h in st.handlers):
                return True
        if isinstance(st, (ast.With, ast.AsyncWith)) and _terminates(st.body):
            return True
    return False


class FuncChecker:
    """对单个函数做确定赋值分析（保守：宁可不报，也不误报）。"""

    def __init__(self, fn: ast.AST, module_names: set[str], filename: str):
        self.fn = fn
        self.module_names = module_names
        self.filename = filename
        self.problems: list[tuple[int, str, str]] = []
        self.assigned: set[str] = set()
        # 参数、global/nonlocal 声明的名字一律当作已赋值
        a = fn.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            self.assigned.add(arg.arg)
        if a.vararg:
            self.assigned.add(a.vararg.arg)
        if a.kwarg:
            self.assigned.add(a.kwarg.arg)
        for node in ast.walk(fn):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                self.assigned.update(node.names)

    # ---- 工具 ----
    def _targets(self, node: ast.AST) -> set[str]:
        out: set[str] = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                out.add(n.id)
        return out

    def _scan_value(self, node: ast.AST) -> None:
        """读一个表达式：里面的 Load 必须已经赋值过。

        要注意几种"看起来像未赋值、其实是局部绑定"的情况，否则误报会淹没真问题：
          · 推导式变量：[f(x) for x in xs] —— x 只在推导式内部有效
          · 函数内 import：def f(): import httpx; httpx.get(...)
          · except ... as e
        """
        # 先把这次表达式里所有推导式/生成式的循环变量收集起来，当作已赋值
        local = set(self.assigned)
        for n in ast.walk(node):
            if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                for gen in n.generators:
                    local |= self._targets(gen.target)
            # lambda 的参数也是它自己的局部名：lambda r: r.get(...) 里的 r
            if isinstance(n, ast.Lambda):
                a = n.args
                for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                    local.add(arg.arg)
                if a.vararg:
                    local.add(a.vararg.arg)
                if a.kwarg:
                    local.add(a.kwarg.arg)
        # 函数内 import 也在这里兜一下（语句级也处理了，双保险）
        for n in ast.walk(node):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    local.add((a.asname or a.name).split(".")[0])

        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                nm = n.id
                if nm in local or nm in self.module_names or nm in BUILTINS:
                    continue
                if nm.startswith("__"):
                    continue
                self.problems.append((
                    getattr(n, "lineno", 0), nm,
                    f"读了未确定赋值的变量 {nm!r}（可能只在某个分支里赋值过）"))

    # ---- 语句级 ----
    def stmts(self, body: list[ast.stmt]) -> None:
        for st in body:
            self.stmt(st)

    def stmt(self, st: ast.stmt) -> None:
        if isinstance(st, ast.Assign):
            self._scan_value(st.value)
            self.assigned |= self._targets_multi(st.targets)
        elif isinstance(st, ast.AugAssign):
            # x += 1 同时是"读"和"写"：读的时候必须已经赋值
            self._scan_value(st.value)
            if isinstance(st.target, ast.Name):
                if st.target.id not in self.assigned \
                        and st.target.id not in self.module_names \
                        and st.target.id not in BUILTINS:
                    self.problems.append((
                        st.lineno, st.target.id,
                        f"`{st.target.id} += ...` 之前没赋过值（这是 extra 那类 bug）"))
                self.assigned.add(st.target.id)
            else:
                self._scan_value(st.target)
                self.assigned |= self._targets(st.target)
        elif isinstance(st, ast.AnnAssign):
            if st.value is not None:
                self._scan_value(st.value)
            if isinstance(st.target, ast.Name):
                self.assigned.add(st.target.id)
        elif isinstance(st, ast.Expr):
            self._scan_value(st.value)
        elif isinstance(st, ast.Return):
            if st.value is not None:
                self._scan_value(st.value)
        elif isinstance(st, ast.If):
            self._scan_value(st.test)
            before = set(self.assigned)
            self.stmts(st.body)
            after_body = set(self.assigned)
            body_stops = _terminates(st.body)
            self.assigned = set(before)
            self.stmts(st.orelse)
            after_else = set(self.assigned)
            else_stops = bool(st.orelse) and _terminates(st.orelse)
            # 哪一边会"就此结束"，就不用参与交集
            if body_stops and else_stops:
                self.assigned = after_body | after_else     # 后面其实到不了，随它
            elif body_stops:
                self.assigned = after_else                  # 只有 else 侧继续
            elif else_stops:
                self.assigned = after_body
            elif st.orelse:
                self.assigned = after_body & after_else
            else:
                self.assigned = before
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            self._scan_value(st.iter)
            before = set(self.assigned)
            self.assigned |= self._targets(st.target)
            self.stmts(st.body)
            self.stmts(st.orelse)
            self.assigned = before      # 循环可能一次都不执行
        elif isinstance(st, ast.While):
            self._scan_value(st.test)
            before = set(self.assigned)
            self.stmts(st.body)
            self.stmts(st.orelse)
            self.assigned = before
        elif isinstance(st, ast.Try):
            before = set(self.assigned)
            self.stmts(st.body)
            after_body = set(self.assigned)
            body_stops = _terminates(st.body)
            # 只要有一个 handler 会"往下走"，它就可能贡献一个不同的绑定状态；
            # 全部 handler 都终止（return/continue/raise）时，
            # try 之后的代码只有"try 成功"这一条路可走 —— 这时 body 的绑定是确定的。
            live: list[set[str]] = []
            for h in st.handlers:
                self.assigned = set(before)
                if h.name:
                    self.assigned.add(h.name)
                if h.type is not None:
                    self._scan_value(h.type)
                self.stmts(h.body)
                if not _terminates(h.body):
                    live.append(set(self.assigned))
            if body_stops and not live:
                # try 和所有 except 都终止了（都 return/raise）—— 后面的语句到不了，
                # 但 **finally 仍然会执行**，它能看到前面所有赋过值的名字。
                # 踩过：这里原来写成 set()（清空），于是
                #     stop = threading.Event()
                #     try: ...; return ...
                #     finally: stop.set()
                # 里的 stop 被误报成"可能未赋值"。
                self.assigned = set(after_body) | set(before)
            elif not live:
                self.assigned = after_body
            else:
                merged = set(after_body)
                for s in live:
                    merged &= s
                self.assigned = merged
            if st.orelse:
                self.stmts(st.orelse)
            self.stmts(st.finalbody)
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            for it in st.items:
                self._scan_value(it.context_expr)
                if it.optional_vars is not None:
                    self.assigned |= self._targets(it.optional_vars)
            self.stmts(st.body)
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 只登记名字，**不要钻进它的函数体** —— 里层有自己的作用域，
            # 用外层的"已赋值集合"去检查它会产生大量误报
            #（例如 def clean(o): ... 里的 o 是它自己的参数）。
            # ast.walk 会另外给每个函数单独建一个 checker。
            self.assigned.add(st.name)
        elif isinstance(st, ast.ClassDef):
            self.assigned.add(st.name)
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            # 函数内 import：import httpx / from x import y as z
            for a in st.names:
                self.assigned.add((a.asname or a.name).split(".")[0])
        elif isinstance(st, ast.Delete):
            for t in st.targets:
                self._scan_value(t)
        elif isinstance(st, (ast.Raise, ast.Assert, ast.Pass, ast.Break, ast.Continue)):
            # 整条语句一起扫 —— 逐个 Name 扫会丢掉推导式/生成式的作用域信息
            #（实测：assert any(..."在" in txt for _w, txt in gui.sent) 里的 txt 被误报）
            self._scan_value(st)
        elif isinstance(st, ast.Match):
            self._scan_value(st.subject)
            before = set(self.assigned)
            results = []
            for case in st.cases:
                self.assigned = set(before)
                self.stmts(case.body)
                results.append(set(self.assigned))
            self.assigned = set.intersection(*results) if results else before
        else:
            # 兜底：把这条语句里所有 Load 都当成"读了"
            for n in ast.walk(st):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    self._scan_value(n)

    @staticmethod
    def _targets_multi(targets: list[ast.AST]) -> set[str]:
        out: set[str] = set()
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    out.add(n.id)
        return out


def check_file(path: pathlib.Path) -> list[tuple[str, int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        return [(str(path), e.lineno or 0, f"语法错误: {e.msg}")]

    module_names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        module_names.add(n.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            module_names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                module_names.add((a.asname or a.name).split(".")[0])

    out = []
    # 建一张"谁是谁的父函数"的表，好把外层作用域的名字喂给闭包，
    # 否则里层函数读外层的变量会被误报（例如 clean() 递归调用自己）。
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def enclosing_names(fn: ast.AST) -> set[str]:
        got: set[str] = set()
        cur = parents.get(id(fn))
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(cur):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                        got.add(n.id)
                    # 外层里定义的嵌套函数/类，闭包可以直接引用它们的名字
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        got.add(n.name)
                    # 外层里 import 的名字，闭包也能用
                    if isinstance(n, (ast.Import, ast.ImportFrom)):
                        for a in n.names:
                            got.add((a.asname or a.name).split(".")[0])
                    # 外层里 import 的名字，闭包也能用
                    #（例如 main() 里 from wechatauto import WeChatGUI，
                    #  里层 gui_factory() 直接 return WeChatGUI()）
                    if isinstance(n, (ast.Import, ast.ImportFrom)):
                        for a in n.names:
                            got.add((a.asname or a.name).split(".")[0])
                a = cur.args
                for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                    got.add(arg.arg)
                if a.vararg:
                    got.add(a.vararg.arg)
                if a.kwarg:
                    got.add(a.kwarg.arg)
            cur = parents.get(id(cur))
        return got

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            c = FuncChecker(node, module_names, str(path))
            c.assigned |= enclosing_names(node)      # 闭包：把外层的名字也算已赋值
            try:
                c.stmts(node.body)
            except RecursionError:
                continue
            for lineno, nm, why in c.problems:
                out.append((str(path), lineno, f"[{node.name}] {why}"))
    return out


def main() -> int:
    base = pathlib.Path(__file__).resolve().parent
    targets = [pathlib.Path(a) for a in sys.argv[1:]] or sorted(base.glob("*.py"))
    allp = []
    for t in targets:
        if not t.is_absolute():
            t = base / t
        allp += check_file(t)

    if not allp:
        print("✅ 没有发现「可能未绑定就使用」的变量。")
        return 0
    print(f"发现 {len(allp)} 处可疑：")
    for f, ln, why in allp:
        print(f"  {pathlib.Path(f).name}:{ln}  {why}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
