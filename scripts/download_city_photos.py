# coding: utf-8
import json, urllib.parse, urllib.request
from pathlib import Path
cities=['Beijing','Shanghai','Xi an','Chengdu','Chongqing','Hangzhou','Tokyo','Bangkok','Singapore','Dubai','Paris','London','New York','San Francisco','Rome','Sydney','Cape Town','Istanbul','Rio de Janeiro']
cn=['北京','上海','西安','成都','重庆','杭州','东京','曼谷','新加坡','迪拜','巴黎','伦敦','纽约','旧金山','罗马','悉尼','开普敦','伊斯坦布尔','里约热内卢']
out=Path('web/img/cities');out.mkdir(parents=True,exist_ok=True); headers={'User-Agent':'travel-planner/1.0'}
for q,name in zip(cities,cn):
 fn=out/(name+'.jpg')
 if fn.exists():continue
 url='https://commons.wikimedia.org/w/api.php?'+urllib.parse.urlencode({'action':'query','generator':'search','gsrsearch':q+' skyline','gsrnamespace':'6','gsrlimit':'1','prop':'imageinfo','iiprop':'url','iiurlwidth':'640','format':'json'})
 try:
  req=urllib.request.Request(url,headers=headers);data=json.load(urllib.request.urlopen(req,timeout=20));pages=(data.get('query') or {}).get('pages') or {};page=next(iter(pages.values()),{});img=((page.get('imageinfo') or [{}])[0].get('thumburl') or '');
  if img:
   req=urllib.request.Request(img,headers=headers);fn.write_bytes(urllib.request.urlopen(req,timeout=20).read());print(name,fn.stat().st_size)
 except Exception as e:print('skip',name,e)
