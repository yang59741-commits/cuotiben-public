# 第三方资源与许可边界

根LICENSE适用于作者原创的软件代码；不覆盖第三方组件或真题资料。

- MathJax（含本地公式渲染资源与配套字体）：保留于 `cuoti_app/static/vendor/mathjax/`，遵循该目录中原有Apache-2.0 LICENSE及上游通知；https://github.com/mathjax/MathJax 。
- PyMuPDF / MuPDF：项目PDF处理依赖，采用GNU AGPL v3或Artifex商业许可。见 https://github.com/pymupdf/PyMuPDF 和 https://pymupdf.readthedocs.io/en/latest/ 。MIT许可不豁免其要求；打包分发或网络提供含此依赖的整体应用时，应满足相应许可条件或取得商业许可。
- Flask、Werkzeug、Jinja2、keyring、customtkinter、Pillow、pystray、certifi及其传递依赖：由依赖文件安装，各自保留原许可证；本仓库未将这些安装包重新许可。
- PyInstaller、pytest、ESLint等构建/开发工具遵循各自上游许可。分发构建产物时应一并保留实际捆绑依赖的许可通知。
- TeX发行版和字体由用户自行安装，依各自许可使用。
- `past-exams/` 是题目转录资料，不适用原创代码MIT授权；准确性与来源限制见其README和根DISCLAIMER。

依赖版本见 `requirements*.txt` 与 `package-lock.json`。第三方许可证以对应版本的上游文件为准。
