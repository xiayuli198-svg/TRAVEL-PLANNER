import json
from pathlib import Path
p=Path('src/travel_planner/data/world_cities.json');d=json.loads(p.read_text(encoding='utf-8'));names={x['name'] for x in d['cities']}
extra=[('首尔','韩国',37.5665,126.978),('开罗','埃及',30.0444,31.2357),('马德里','西班牙',40.4168,-3.7038),('阿姆斯特丹','荷兰',52.3676,4.9041),('温哥华','加拿大',49.2827,-123.1207),('洛杉矶','美国',34.0522,-118.2437),('墨尔本','澳大利亚',-37.8136,144.9631),('德里','印度',28.6139,77.209),('里斯本','葡萄牙',38.7223,-9.1393),('布拉格','捷克',50.0755,14.4378),('维也纳','奥地利',48.2082,16.3738),('伊斯坦布尔','土耳其',41.0082,28.9784)]
for n,c,lat,lon in extra:
 if n not in names:d['cities'].append({'name':n,'country':c,'lat':lat,'lon':lon,'tags':['夜景','城市漫游'],'best_time':'春秋','photo':n})
p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
