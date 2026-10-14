# 可选：让电脑不要睡眠，保证早上服务在线。
# 用法（可能需要管理员权限的 PowerShell）：powershell -ExecutionPolicy Bypass -File .\set-no-sleep.ps1
# 恢复默认：  powercfg /change standby-timeout-ac 30

Write-Host "当前电源方案："
powercfg /getactivescheme

Write-Host "`n设置为：永不睡眠 / 永不休眠 / 硬盘不关，显示器 30 分钟关闭。"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0
powercfg /change monitor-timeout-ac 30

Write-Host "`n完成。如果你用笔记本并且要合盖使用，还需要把「合盖时」的动作改成「不采取任何操作」：" -ForegroundColor Yellow
Write-Host "  控制面板 → 电源选项 → 选择关闭盖子的功能 → 接通电源时：不采取任何操作"
