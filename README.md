# nginx-tui 🚀

一个用 Python 编写的、无任何第三方依赖的轻量级命令行终端 TUI 浏览器和下载器。专门用于浏览和下载开启了 `autoindex` 的 Nginx 静态文件服务器。

## ✨ 特性

- **零依赖**：仅使用 Python 3 标准库（如 `curses`, `urllib`），无需通过 `pip` 安装任何第三方依赖包。
- **终端图形界面 (TUI)**：支持在终端中使用键盘方向键、回车键等直接浏览远程目录结构。
- **安全临时下载**：采用 `.part` 临时文件暂存机制，只有在下载完全成功后才会原子替换目标文件，若中途失败或取消会自动清理，防止损坏已有的文件。
- **下载进度展示**：显示下载百分比、可视进度条、已下载大小、文件总大小以及已用时间。
- **安全与控制**：支持通过 `Ctrl-C` 随时安全取消下载任务。
- **HTTPS 证书跳过**：提供 `-k` / `--insecure` 参数以跳过 HTTPS 自签名证书校验。

## 📦 安装与运行

直接克隆或下载脚本即可运行：

```bash
# 赋予执行权限
chmod +x nginx_tui.py

# 运行（以浏览某个 nginx autoindex 目录为例）
./nginx_tui.py http://example.com/files/
```

### 命令行参数

```text
用法: nginx_tui.py [-h] [-o OUTPUT_DIR] [-k] url

浏览并下载开启了 autoindex 的 nginx 静态文件服务器目录中的文件（终端 TUI）。

位置参数:
  url                   nginx autoindex 目录列表的 URL

选项:
  -h, --help            显示帮助信息并退出
  -o OUTPUT_DIR, --output-dir OUTPUT_DIR
                        下载文件保存的本地目录（默认：当前工作目录）
  -k, --insecure        跳过 HTTPS 证书校验
```

## ⌨️ 快捷键

- `↑` / `↓` 或 `k` / `j`：上下移动光标
- `PageUp` / `PageDown`：向上/向下翻页
- `Enter` (回车)：**进入**选中的目录，或**下载**选中的文件
- `→` (右方向键)：**进入**选中的目录
- `←` (左方向键) 或 `Backspace` (退格键) 或 `u` 或 `Esc`：**返回**上一级目录
- `r` / `R` 或 `F5`：**刷新**当前目录文件列表
- `q` / `Q`：**退出**程序

## 📄 开源协议

本项目采用 MIT 协议开源。
