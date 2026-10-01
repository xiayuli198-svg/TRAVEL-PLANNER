import urllib.request
from pathlib import Path
imgs={'纽约':'photo-1485871981521-5b1fd3805eee','旧金山':'photo-1501594907352-04cda38ebc29','罗马':'photo-1529260830199-42c24126f198','悉尼':'photo-1506973035872-a4ec16b8e8d1','开普敦':'photo-1516026672322-bc52d61a55d5','伊斯坦布尔':'photo-1524231757912-21f4fe3a7200'}
out=Path('web/img/cities');
for n,i in imgs.items():
 try:
  req=urllib.request.Request(f'https://images.unsplash.com/{i}?w=640&q=80',headers={'User-Agent':'Mozilla/5.0'});out.joinpath(n+'.jpg').write_bytes(urllib.request.urlopen(req,timeout=20).read());print(n)
 except Exception as e:print(e)
