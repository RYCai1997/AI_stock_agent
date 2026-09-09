# v1.0.2 Windows启动修复

本补丁修复`Launch_Stock_System.bat`在Windows双击时因换行格式导致命令被截断、GUI无法打开的问题。

启动器现在会检查运行依赖：项目虚拟环境完整时优先使用；不完整时尝试全局Python；两者均不可用时显示明确提示并建议重新运行`Install_Stock_System.bat`。

本次没有改变`A_CSI300_QVM_TIMING_V1`策略、数据、仓位或退出规则。
