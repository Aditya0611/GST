import re

with open('static/dashboard.html', 'r', encoding='utf-8') as f:
    content = f.read()

style_match = re.search(r'<style>(.*?)</style>', content, re.DOTALL)
if style_match:
    style_content = style_match.group(1).strip()
    with open('static/dashboard.css', 'w', encoding='utf-8') as f:
        f.write(style_content)
    print('Extracted CSS')
    
    new_content = content[:style_match.start()] + '<link rel="stylesheet" href="/static/dashboard.css">' + content[style_match.end():]
    with open('static/dashboard.html', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print('Updated HTML CSS link')
