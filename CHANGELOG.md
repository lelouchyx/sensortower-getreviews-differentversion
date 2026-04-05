# 变更历史（Changelog）

## [1.0.0] - 2026-03-31

### 新增功能
- ✨ **Streamlit Web UI**: 完整的图形化界面，支持侧边栏配置 + 制表符结果展示
- 🌐 **双平台支持**: iOS App Store + Google Play 统一查询接口
- 🔄 **自动翻译**: GoogleTranslator 集成，3-strike 容错机制
- 🎨 **4 张词云**: 高评/低评 × iOS/Android 分组展示
- 🧹 **智能噪声词**:
  - 品牌词动态过滤（按搜索关键词自动过滤）
  - 自动噪声建议（跨组高频副词检测）
  - 手工停用词编辑（支持逗号/空格/换行分隔）
  - 一键加入 + 逐个选择
- 💾 **零 API 重复调用**: 第一次查询缓存所有数据，后续停用词调整无需重新请求 API
- 📊 **诊断信息**: 翻译失败率、非中文占比显示
- 📱 **命令行工具**: `main.py` 支持参数化查询
- 🧪 **快速测试脚本**: `test_query.py` 用于离线验证

### 改进
- 翻译器改为"每条重试 + 失败回退"，取消全局禁用逻辑
- 扩充中英双语停用词表
- 支持 `extra_stopwords` 参数传递到词频统计
- 改进错误消息，显示重试次数和失败原因

### 修复
- 修复 `test_query.py` 计数逻辑（iOS/Android 计数器独立）
- 改进 jieba 字典构建错误处理

### 文档
- 完整 README.md（快速开始、参数说明、工作流、FAQ）
- .gitignore 配置（Python、Streamlit、输出文件）
- LICENSE (MIT)
- CHANGELOG.md 本文件

## 内部版本（Pre-1.0.0）

### 2026-03-30 版
- 基础 SensorTower API 客户端（iOS/Android 双端点）
- 评论分词与停用词过滤
- 词云生成管道

### 2026-03-25 版
- 初始项目框架
- Settings / Review / SearchRequest 数据模型
