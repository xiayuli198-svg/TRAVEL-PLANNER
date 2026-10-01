from pathlib import Path
p=Path('web/globe3d.js');s=p.read_text(encoding='utf-8');s=s.replace("['德里',77,29]]", "['德里',77,29],['里斯本',-9,39],['布拉格',14,50],['维也纳',16,48]]");p.write_text(s,encoding='utf-8')
