import sys

with open('scripts/test_ui_e2e.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('✓', '[OK]')
content = content.replace('✅', '[SUCCESS]')
content = content.replace('⚠', '[WARN]')
content = content.replace('❌', '[X]')
content = content.replace('✗', '[X]')
content = content.replace('📸', '[SCREENSHOT]')

with open('scripts/test_ui_e2e.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Replaced Unicode characters")
