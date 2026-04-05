# SensorTower Review Analyzer (Different Versions)

面向版本归因场景的 SensorTower 评论分析工具。

核心目标：
- 按时间段和版本映射聚合评论
- 聚焦养成相关关键词做版本对比
- 输出可离线复用的 CSV，避免重复消耗 API

## 功能概览

- Streamlit 可视化界面，支持单平台和双平台
- SensorTower API 抓取 iOS/Android 评论
- CSV 导入分析（无需再次调用 API）
- 版本时间映射（版本名 + 起止日期）
- 养成相关关键词命中统计
- 评论语义精简导出
- 版本归因指标和强度对比
- 词云图（高评/低评）

## 安装与运行

1. 安装依赖

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

2. 启动应用

streamlit run app.py

默认地址： http://localhost:8501

## 两种数据来源

### 1) SST API 抓取

- 在侧边栏填写 API Key
- 选择 App、日期、版本映射、关键词和参数
- 点击 开始查询

### 2) 导入 CSV 分析

- 在侧边栏将 数据来源 切到 导入CSV分析
- 上传 CSV 后点击 开始查询
- 会直接复用同一套分析，不访问 API

## CSV 兼容格式（离线复刻推荐）

为了离线复刻 API 分析结论，建议 CSV 至少包含以下字段：

- 平台
- 评分
- 评论时间
- 归属版本
- 原始评论

当前程序导出的语义 CSV 已升级为包含完整字段：

- 平台
- 评分
- 评论时间
- 归属版本
- 原始评论
- 精简评论
- 养成方向的精简评论
- 命中关键词

## 输出文件

运行后会在 outputs_ui_latest 目录生成：

- reviews_semantic.csv
- reviews_raw_before_dedupe.csv
- ios_high.png
- ios_low.png
- android_high.png
- android_low.png

## 关键口径说明

- 版本归因区域当前为全量入表展示（不按阈值过滤）
- 命中统计按原始评论关键词匹配
- API 请求会在以下条件任一达成时停止：
  - 达到目标命中数
  - 达到最大 API 请求次数
  - 到达最后页
  - 无新增数据

## 安全与共享

请务必在公开仓库前检查：

- 不提交任何真实 API Key 或 token
- 不提交 .env
- 不提交运行产物 CSV、缓存、日志

本仓库已在 .gitignore 中默认忽略：

- .env 和 .env.*
- outputs_ui_latest/
- reviews_semantic_*.csv
- debug.log
- .streamlit/secrets.toml

建议推送前执行：

git status --short
git diff --staged

## 项目结构

- app.py：Streamlit 主应用
- src/sst_search/sst_client.py：SensorTower API 客户端
- src/sst_search/review_semantic.py：语义精简和关键词命中
- src/sst_search/analyzer.py：词频和评分分组
- src/sst_search/models.py：数据模型
