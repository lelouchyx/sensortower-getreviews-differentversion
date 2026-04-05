import requests
import json

# 查询 iOS App ID
print('=== 查询 iOS App ID ===')
try:
    ios_response = requests.get('https://itunes.apple.com/search?term=kimi&entity=software&country=US', timeout=10)
    ios_data = ios_response.json()
    
    if ios_data['resultCount'] > 0:
        for app in ios_data['results'][:5]:
            print(f"应用名: {app.get('trackName', 'N/A')}")
            print(f"App ID: {app.get('trackId', 'N/A')}")
            print(f"发布者: {app.get('sellerName', 'N/A')}")
            print(f"版本: {app.get('version', 'N/A')}")
            print('---')
    else:
        print('未找到相关应用')
except Exception as e:
    print(f'iOS 查询失败: {e}')

print('\n=== Google Play 查询提示 ===')
print('Google Play 不提供公开 API，需要手动查询或使用爬虫工具')
print('方案：')
print('1. 访问 https://play.google.com/store/apps/details?id=<包名>')
print('2. 或在搜索框搜索 "Kimi" 获取完整包名')
print('3. 通常 Kimi 的包名为：com.zhipu.kimi 或类似格式')
