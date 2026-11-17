from pathlib import Path
import re, json
source = Path('index-old.html').read_text(encoding='utf-8-sig')
match = re.search(r'const\s+SERVICES\s*=\s*', source)
if not match:
    raise SystemExit('Не найден const SERVICES в index-old.html')
data, end = json.JSONDecoder().raw_decode(source[match.end():].lstrip())
if not isinstance(data, list) or not data:
    raise SystemExit('Каталог пуст или неверного формата')
Path('services.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
print('Сохранено услуг:', len(data))
