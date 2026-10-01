from pathlib import Path
p=Path('web/globe3d.js');s=p.read_text(encoding='utf-8');needle="const L={'欧亚经典环线'";idx=s.index(needle);end=s.index(";let g",idx);l=s[idx:end];insert="const LOOP_INFO={'欧亚经典环线':'东京霓虹、东南亚夜市、迪拜天际线与欧洲古城串联，适合 14–21 天。','欧洲西部环线':'伦敦—巴黎—罗马—伊斯坦布尔，博物馆、古迹与夜景密度最高，适合 10–14 天。','北美太平洋环线':'旧金山—纽约—墨西哥城，城市文化与海岸景观结合，适合 12–18 天。','环球探索环线':'跨越亚欧美非大洋洲的长线旅行，建议分段完成，适合 30 天以上。'};"
s=s[:end+1]+insert+s[end+1:]
s=s.replace("s.onchange=()=>{active=[...L[s.value]];update()}","s.onchange=()=>{active=[...L[s.value]];byId('globe-info').textContent=LOOP_INFO[s.value];update()}")
p.write_text(s,encoding='utf-8')
