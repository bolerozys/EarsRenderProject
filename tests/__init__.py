# -*- coding: utf-8 -*-
"""测试包。

把 ``tests`` 变成真正的包（而不是靠 sys.path 兜着的目录），
这样两种调用方式都能正常工作：

    python -m unittest discover -s tests          # 从 tests 目录开始
    python -m unittest discover -s tests -t .     # 显式指定仓库根为 top-level

否则在某些 Python 版本上会直接报
``ImportError: Start directory is not importable: 'tests'``。
"""
