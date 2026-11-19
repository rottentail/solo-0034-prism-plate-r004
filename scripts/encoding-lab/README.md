# encoding-lab：PNG编码升级评估实验台

ADR-0001（docs/adr-0001-png-encoding-identity.md）的决策支持工具，非交付运行代码。

```sh
python3 scripts/encoding-lab/run.py            # 全部六个阶段，结果写入临时目录并打印路径
python3 scripts/encoding-lab/run.py --work /tmp/encoding-lab   # 指定保留目录
GO=/path/to/go python3 scripts/encoding-lab/run.py             # 指定Go工具链
```

run.py 从仓库源码构建两个真实服务二进制：binA为现状；binB用 `go build -overlay`
在构建期把 internal/raster/raster.go 的PNG编码调用替换为更高压缩级别（仅序列化变化，
像素管线不动），模拟压缩器升级。随后经真实HTTP完成基线、同根升级、微变化、
封包重渲染、转码等价与512×512边界六组实验，输出 facts.json 与PNG样本。
不修改仓库源码，不触碰任何既有数据根。pngtool.go 仅用Go标准库做PNG解码、
重编码与像素比较，与服务同源。
