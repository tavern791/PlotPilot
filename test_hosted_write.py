#!/usr/bin/env python3
"""测试托管连写端点（auto_save=true）"""
import requests
import json

novel_id = "test-quality-1"
url = f"http://localhost:8007/api/v1/novels/{novel_id}/hosted-write-stream"

payload = {
    "from_chapter": 1,
    "to_chapter": 1,
    "auto_save": True,
    "auto_outline": True
}

print(f"POST {url}")
print(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
print("\n--- Streaming response ---\n")

response = requests.post(url, json=payload, stream=True)
print(f"Status: {response.status_code}")

if response.status_code == 200:
    for line in response.iter_lines():
        if line:
            line_str = line.decode('utf-8')
            if line_str.startswith('data: '):
                data = json.loads(line_str[6:])
                event_type = data.get('type')

                if event_type == 'session':
                    print(f"[SESSION] {data}")
                elif event_type == 'chapter_start':
                    print(f"[CHAPTER_START] Chapter {data.get('chapter')}")
                elif event_type == 'outline':
                    print(f"[OUTLINE] {data.get('text', '')[:100]}...")
                elif event_type == 'phase':
                    print(f"[PHASE] {data.get('phase')}")
                elif event_type == 'chunk':
                    pass  # 跳过内容片段
                elif event_type == 'done':
                    content_len = len(data.get('content', ''))
                    print(f"[DONE] Content length: {content_len}")
                elif event_type == 'saved':
                    print(f"[SAVED] Chapter {data.get('chapter')}, ok={data.get('ok')}, created={data.get('created', False)}")
                    if not data.get('ok'):
                        print(f"  Error: {data.get('message')}")
                elif event_type == 'error':
                    print(f"[ERROR] {data.get('message')}")
else:
    print(f"Error: {response.text}")
