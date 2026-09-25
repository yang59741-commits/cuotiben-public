# 错题本

本地运行的数学错题整理、间隔复习与试卷工具。支持 LaTeX 公式、题目分类、复习记录、PDF 导出和可自行配置的 AI 辅助。项目源码与模块化测试集中维护在这一份仓库，可以自行修改、扩展。

## 资料准确性声明

**本仓库真题资料尚未逐题进行人工校验，题面转录、公式、分题及排版可能存在错误，不保证正确性与完整性。使用前请自行核对原始试卷。AI 生成的分析、答案和试卷同样可能出错，不能替代人工判断。**

`past-exams/` 提供368份按学校、科目和年份整理的TeX资料，不包含湖南师范大学、大连理工大学试题。学校和年份是资料整理标签，不代表学校授权、官方发布或已经核实来源。详见 [资料说明](past-exams/README.md) 和 [声明](DISCLAIMER.md)。

## 安装与启动

建议使用独立 Python 3.11 环境。Windows建议将项目放在较短目录，避免备份或TeX生成时触发系统路径长度限制。Windows桌面启动器需要图形环境；源码Web服务绑定本机 `127.0.0.1:8080`。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

浏览器打开 http://127.0.0.1:8080 。Windows也可以运行 `python launcher.py` 或双击 `启动.bat`。不要同时启动多个入口。第一次启动自动创建空白的 `data/cuoti.db`（schema63）及本机运行目录，不需要作者的数据库、题库索引或任何备份。

- 基础错题和复习功能不要求AI服务。AI功能需自行在设置页面配置兼容服务、模型和凭据；凭据不在仓库内，调用可能产生服务商费用。启用AI时相关题目内容会发送到你配置的服务。
- 真题目录默认指向仓库内 `past-exams/`。普通题库默认未配置，可通过 `CUOTI_TEX_BASE` 指定自己有权使用的TeX题库；也可设置 `CUOTI_PAST_EXAM_DIR` 覆盖真题路径。
- PDF功能需要单独安装TeX Live或MiKTeX，并确保 `xelatex` 在PATH中。使用中文、数学相关宏包及合适字体；`python tools/check_environment.py` 可检查环境。
- `instance/settings.example.json` 是非敏感设置示例；实际正式布局设置位于 `data/runtime/settings.json`。默认学校为空，可按自己需求配置；部分高级真题组卷/语义匹配功能需要自行建立索引和画像，仓库不含作者预生成材料。
- 本地服务未提供面向公网的身份认证，不要直接作为公网服务部署。

## 完整测试工具

只保留一套 `tests/`，由统一入口运行；测试使用小型合成fixture，不需要个人数据库或真实AI凭据。

```powershell
python -m pip install -r requirements-dev.txt
npm ci
python tools/check_project.py --full
```

不加 `--full` 运行启动、复习、迁移及前端的定向检查。`--full` 按文件运行全部Python测试并运行全部前端合同和数学字体检查，日志写入被忽略的 `output/checks/`。单测：`python -m pytest -q tests/test_spaced_review_summary.py`。性能测试入口为 `python tools/performance_runner.py --help`，使用合成数据；有意保持为可选项，避免日常检查生成大库。

测试包括复习保存与幂等、分页/筛选、公式资源、导入导出、数据库迁移与回滚、任务恢复、PDF管线、启动互斥、数据备份恢复和前端交互合同。旧的专项实验与一次性真实数据修复脚本不属于公开测试集合。浏览器fixture仅为测试辅助代码，不包含作者隔离库。

## 开发与构建

入口为 `app.py`；`cuoti_app/routes/` 提供HTTP接口，`services/` 实现业务规则，`migrations/` 管理数据库版本，`static/js/` 与 `templates/` 是前端，`tests/` 是统一测试集合。修改持久化行为时同时补充迁移和相应测试；不要提交运行数据或凭据。

Windows自行构建EXE：安装 `requirements-build.txt` 后按 `tools/build_launcher.ps1` 参数构建，或使用 `python -m PyInstaller --noconfirm --clean packaging/cuoti_launcher.spec`。源码发行不捆绑Python环境或作者的本地EXE；分发打包程序前请阅读第三方许可，特别是PyMuPDF的AGPL/商业许可。

## 已知限制与许可

源码已包含复习提交后的局部更新，避免为未变化题目重新创建整页公式。仍有尚未定位的原生运行时/SQLite间歇异常；不能把定向测试或一次通过视为所有环境稳定性的保证。重要个人数据请自行备份。源代码公开不提供题目、答案、AI输出或软件无故障保证。

原创项目代码采用 [MIT](LICENSE)。真题资料和第三方组件不被本项目重新授予MIT许可，分别遵循资料说明和 [第三方许可](THIRD_PARTY_NOTICES.md)。本仓库使用全新提交历史，不含个人学习数据、普通题库、预生成私有材料、备份、内部计划、历史日志及实验库。
