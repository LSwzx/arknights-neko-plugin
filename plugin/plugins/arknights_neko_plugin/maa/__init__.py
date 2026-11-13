"""MAA 引擎子包：wrapper（ctypes 绑定）/ errors（异常体系）/ adapter / runtime / provision。

本文件刻意留空：任何 import 都会让 ``maa`` 的导入期副作用扩散到不需要它的模块，
且会让 ``provision`` 这类"纯标准库、可脱宿主单测"的模块被迫拖入宿主依赖。
"""
