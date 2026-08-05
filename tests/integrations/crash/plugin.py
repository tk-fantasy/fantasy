"""启动即退出的测试插件（模拟握手前崩溃）。

不做握手响应 → 宿主侧 call(handshake) 超时 → 启动失败。
"""

import sys


def main():
    # 什么都不做，直接退出（进程退出码 1，stdout 无握手响应）
    sys.exit(1)


if __name__ == "__main__":
    main()
