from __future__ import annotations
import copy
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.base_client import BaseShadowClient
from core.token_store import TokenStore

class HepsiburadaClient(BaseShadowClient):
    def __init__(self, tenant_id: str = "default", token_store: TokenStore | None = None):
        super().__init__(service_name="hepsiburada", base_url="https://www.hepsiburada.com", tenant_id=tenant_id, token_store=token_store)

    def send_sensor_data(self, sensor_data: str = "3;0;1;0;4538948;ghw2CIYdpNCY08HE4PRABur6ayRKpSayw1SGBq605Rs=;126,0,0,4,9,0;t(eRjkVl\"2.\"H%#L =B5j\"v^ PQ\"{?\"1;Y\"Qrk[#\"DLk\"_P|jq\"^L~\".\"WL1(?\"r8C\"v@X4D\"<\"SkT_gLm1oj#\"^6\"%}R!#a\"vvm%JZ\"\"&\"~,%\"/\"K057Y\"kP7\"X7;\"-\"4>D%aVMfl_V JZZMYQJ?fe@GooK&LqeJj0Jq@cl!w`%[\"d\" b5\"b(PzX5tsJDPDf@0Cu\",s;\",\"\"DaA\"VL@\"[\"\"[\"(@2\"Q=p/3J=KTVls/Vzf0yP^RW4\"6\"dns\"@@6@iJ]dR*@3W[|>S\"lh2\"p =}GR:\"9KN\"wtB3A\"5{k}\"*H[99\"^kZ@\"9}LWK\"Rkz,\"V7a0I\"m(1V\"t0Vj))lSHf}fUF\"[W\"`xo[B+&E4/3:j}\"#Cg\" 2x|\"-.F6\">Y\"x&0\"_-P\"sz5k5dgy\"Uf7\"N\"\".&(\"FfI\">%\"3BqN\"?YaJo\"uU3\"K\".O$FJ:O0[qIY-e@?2nPOXPn$^M oRzrT[(^6%pNk0I?;mv9 4v:VgN|`H3mj%qV1FH <C(TrD% f@i8QWTSx%Tt}lh?>v`4il{*~/6w&,Z?vAk8\"F2l\"2]#\")\"o\"B>`\"@Z3\"nd2AzCpcq8t{?|k,=UM\"|ia\"}hF\"zH#\"c];\"/vN\"Cf0RZ\"9O}\"WT5\"w(r\"rU_6p\"+4s\"^+|@64f+E42[dd0\"y%+\"JL,L$?Cg\"6\"?my-h^;{,^O\"W2.\"7\"G\"D\"<ph\"*+z\"T.p\"|yJT \",L\"!\"zra\"d\"^v!\"K3U9%\"Zn\"x\"\"U\",!l\"10wf0\"jx~;\"<\"i+oRa:uwGT=G.B\"5\"L&Y\"=qQCJN\"muPO\"-\"_;JaAjqV^Tpb{\"Y\"X^v\"^]Gry\"<#J\"gs?/=\" Pd\"*~(%5\"{O\"y\"{M^tY|Y9Xm9u(mTF5}WJ!mo-+tUeU7ZQA--l-D!An9>?a=]E_`BGy&3\"a\"Nd<\"2C1{4\"^/),;Wx&\"Q~BG\"__|m8N0\"9\"q@_\">SZ\"<\"9zGt,TTDs8-}7>\"qGK\"$ l\"tdz\"*?.Q\",r9]9[ZX!\"8(\" \"`8F 0LEE\"5D?\"iK#\"z\"\"g\"2D?\"qldv>y<\"\"a\"l}t\"T\"\".\"vG4\"<q;k:\"EyO\"%BYAbGcNaixs\"#GOm\"_Xx\";qp\";\")qJUcTE+N?kgn&<V\"8\"<yU\"H\"50ik\"Z|v\">^i\")\"\"qNv\"mw)\"3;P\"<7%\"D\"\"s\"l#Q\"S.Y\"I=q\"jWwSIwY\"`!X\"a\":|\"2\"CBD\":\";X0_N2)eI%fs@8!Qv\"1\"wv$\"A\"|k\"NI5\"rQs$\"(dT jz\"||W \"y~j_tv:\"\"n\"UJQ\"l\"|\"X`3\"gJ`\"lh9aD?;\"HAn\"7,|Iz#%M|vVV@4.~\"+t-\"l\"\"j\"ISh\"dgs\"N#]]\"n4|Xk\"P;\"R\"xH\"ZD`\"Rfr?\";7W\"/q9\"9e8\"a]R\":2~}?0:\"xRPWi(S\"X]#PUQPg _=7s7~\"7D*\"5\"UwfFP~i$UhBV$#%vVz9zw,-0yW4gAn)<1m8C7Z^FMAI,<eK[\"d1q\"TQX\"Z\"R][M 4R_?c)K&W\"r\"Hd \"x\"A88xp\"oIA\"YlY\"Ag^\"Fb=\"l\"g8o|\"2\"$e^\"*%G\"vyV\"?\"\"<Ko\"vO\"W\"fSz^p96;g=:$=T}@w\"Z\"Z^]\"%yqH\"(WMz\"l\"\"*\"eD\"") -> dict[str, Any]:
        headers = {
            "content-type": "text/plain;charset=UTF-8",
            "sec-ch-ua-platform": "\"Windows\"",
            "referer": "https://www.hepsiburada.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
            "sec-ch-ua": "\"Not:A-Brand\";v=\"8\", \"Chromium\";v=\"156\", \"Google Chrome\";v=\"156\"",
            "sec-ch-ua-mobile": "?0"
        }
        return self.request(method="POST", url="/tp85CVo3/IOuS42J/5VSVkyO/Rd/kiiuLJ8VJQfftt/UApRcXVcJg/UQp9FT/ZZREA", headers=headers, json_payload={"sensor_data": sensor_data})


def send_sensor_data(sensor_data: str = "3;0;1;0;4538948;ghw2CIYdpNCY08HE4PRABur6ayRKpSayw1SGBq605Rs=;126,0,0,4,9,0;t(eRjkVl\"2.\"H%#L =B5j\"v^ PQ\"{?\"1;Y\"Qrk[#\"DLk\"_P|jq\"^L~\".\"WL1(?\"r8C\"v@X4D\"<\"SkT_gLm1oj#\"^6\"%}R!#a\"vvm%JZ\"\"&\"~,%\"/\"K057Y\"kP7\"X7;\"-\"4>D%aVMfl_V JZZMYQJ?fe@GooK&LqeJj0Jq@cl!w`%[\"d\" b5\"b(PzX5tsJDPDf@0Cu\",s;\",\"\"DaA\"VL@\"[\"\"[\"(@2\"Q=p/3J=KTVls/Vzf0yP^RW4\"6\"dns\"@@6@iJ]dR*@3W[|>S\"lh2\"p =}GR:\"9KN\"wtB3A\"5{k}\"*H[99\"^kZ@\"9}LWK\"Rkz,\"V7a0I\"m(1V\"t0Vj))lSHf}fUF\"[W\"`xo[B+&E4/3:j}\"#Cg\" 2x|\"-.F6\">Y\"x&0\"_-P\"sz5k5dgy\"Uf7\"N\"\".&(\"FfI\">%\"3BqN\"?YaJo\"uU3\"K\".O$FJ:O0[qIY-e@?2nPOXPn$^M oRzrT[(^6%pNk0I?;mv9 4v:VgN|`H3mj%qV1FH <C(TrD% f@i8QWTSx%Tt}lh?>v`4il{*~/6w&,Z?vAk8\"F2l\"2]#\")\"o\"B>`\"@Z3\"nd2AzCpcq8t{?|k,=UM\"|ia\"}hF\"zH#\"c];\"/vN\"Cf0RZ\"9O}\"WT5\"w(r\"rU_6p\"+4s\"^+|@64f+E42[dd0\"y%+\"JL,L$?Cg\"6\"?my-h^;{,^O\"W2.\"7\"G\"D\"<ph\"*+z\"T.p\"|yJT \",L\"!\"zra\"d\"^v!\"K3U9%\"Zn\"x\"\"U\",!l\"10wf0\"jx~;\"<\"i+oRa:uwGT=G.B\"5\"L&Y\"=qQCJN\"muPO\"-\"_;JaAjqV^Tpb{\"Y\"X^v\"^]Gry\"<#J\"gs?/=\" Pd\"*~(%5\"{O\"y\"{M^tY|Y9Xm9u(mTF5}WJ!mo-+tUeU7ZQA--l-D!An9>?a=]E_`BGy&3\"a\"Nd<\"2C1{4\"^/),;Wx&\"Q~BG\"__|m8N0\"9\"q@_\">SZ\"<\"9zGt,TTDs8-}7>\"qGK\"$ l\"tdz\"*?.Q\",r9]9[ZX!\"8(\" \"`8F 0LEE\"5D?\"iK#\"z\"\"g\"2D?\"qldv>y<\"\"a\"l}t\"T\"\".\"vG4\"<q;k:\"EyO\"%BYAbGcNaixs\"#GOm\"_Xx\";qp\";\")qJUcTE+N?kgn&<V\"8\"<yU\"H\"50ik\"Z|v\">^i\")\"\"qNv\"mw)\"3;P\"<7%\"D\"\"s\"l#Q\"S.Y\"I=q\"jWwSIwY\"`!X\"a\":|\"2\"CBD\":\";X0_N2)eI%fs@8!Qv\"1\"wv$\"A\"|k\"NI5\"rQs$\"(dT jz\"||W \"y~j_tv:\"\"n\"UJQ\"l\"|\"X`3\"gJ`\"lh9aD?;\"HAn\"7,|Iz#%M|vVV@4.~\"+t-\"l\"\"j\"ISh\"dgs\"N#]]\"n4|Xk\"P;\"R\"xH\"ZD`\"Rfr?\";7W\"/q9\"9e8\"a]R\":2~}?0:\"xRPWi(S\"X]#PUQPg _=7s7~\"7D*\"5\"UwfFP~i$UhBV$#%vVz9zw,-0yW4gAn)<1m8C7Z^FMAI,<eK[\"d1q\"TQX\"Z\"R][M 4R_?c)K&W\"r\"Hd \"x\"A88xp\"oIA\"YlY\"Ag^\"Fb=\"l\"g8o|\"2\"$e^\"*%G\"vyV\"?\"\"<Ko\"vO\"W\"fSz^p96;g=:$=T}@w\"Z\"Z^]\"%yqH\"(WMz\"l\"\"*\"eD\"", tenant_id: str = "default", mode: str = "local") -> dict[str, Any]:
    """Kullanıcıdan alınan sensör verilerini gönderir."""
    client = HepsiburadaClient(tenant_id=tenant_id)
    return client.send_sensor_data(sensor_data=sensor_data)
