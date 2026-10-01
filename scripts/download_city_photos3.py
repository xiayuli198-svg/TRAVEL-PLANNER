import urllib.request
from pathlib import Path
imgs={'北京':'photo-1548919973-5cef591cdbc9','东京':'photo-1540959733332-eab4deabeeaf','曼谷':'photo-1508009603885-50cf7c579365','新加坡':'photo-1525625293386-3f8f99389edd','迪拜':'photo-1512453979798-5ea266f8880c','巴黎':'photo-1502602898657-3e91760cbb34','伦敦':'photo-1513635269975-59663e0ac1ad','纽约':'photo-1496588152823-86ff7695e68f','旧金山':'photo-1501594907352-04cda38ebc29','罗马':'photo-1529260830199-42c24126f198','悉尼':'photo-1506973035872-a4ec16b8e8d1','开普敦':'photo-1516026672322-bc52d61a55d5','伊斯坦布尔':'photo-1524231757912-21f4fe3a7200','洛杉矶':'photo-1534190760961-74e8c1c5c3da','温哥华':'photo-1559511260-66a654ae982a','墨尔本':'photo-1514395462725-fb4566210144','首尔':'photo-1538485399081-7c897e7b2f27','开罗':'photo-1568322445389-f64ac2515020','马德里':'photo-1539037116277-4db20889f2d8','阿姆斯特丹':'photo-1534351590666-13e3e96b5017'}
out=Path('web/img/cities');out.mkdir(parents=True,exist_ok=True)
for n,i in imgs.items():
 try:
  req=urllib.request.Request(f'https://images.unsplash.com/{i}?w=640&q=82&auto=format',headers={'User-Agent':'Mozilla/5.0'});data=urllib.request.urlopen(req,timeout=25).read();
  if len(data)>10000:(out/(n+'.jpg')).write_bytes(data);print(n,len(data))
 except Exception as e: print('skip',n,e)
