"""Build the TV reference fixture from captured data.

Run once to create the fixture file. Not part of CI.
"""

import json
from pathlib import Path

FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "tv_reference_btcusdt_1h.json"

# OHLCV bars captured from TradingView BTCUSDT 1H on 2026-04-12
# via data_get_ohlcv(count=200)
# Indicator values captured simultaneously via data_get_study_values()
BARS = [
    {"time": 1775296800, "open": 66988.06, "high": 67167, "low": 66918, "close": 67167, "volume": 290.85424},
    {"time": 1775300400, "open": 67167, "high": 67232.72, "low": 67083.43, "close": 67156.93, "volume": 363.18025},
    {"time": 1775304000, "open": 67156.93, "high": 67185, "low": 67060.56, "close": 67097.87, "volume": 140.59027},
    {"time": 1775307600, "open": 67097.87, "high": 67267, "low": 67082.41, "close": 67193.44, "volume": 362.06124},
    {"time": 1775311200, "open": 67193.45, "high": 67223.51, "low": 67029.07, "close": 67215.24, "volume": 369.07128},
    {"time": 1775314800, "open": 67215.24, "high": 67562.93, "low": 67177.39, "close": 67383.66, "volume": 635.57272},
    {"time": 1775318400, "open": 67383.66, "high": 67520.85, "low": 67331.06, "close": 67366.68, "volume": 294.26452},
    {"time": 1775322000, "open": 67366.68, "high": 67416.48, "low": 67292, "close": 67329.4, "volume": 254.01236},
    {"time": 1775325600, "open": 67329.41, "high": 67380.98, "low": 67265, "close": 67295.47, "volume": 120.36154},
    {"time": 1775329200, "open": 67295.47, "high": 67543.6, "low": 67275, "close": 67288.42, "volume": 189.77457},
    {"time": 1775332800, "open": 67288.42, "high": 67295.81, "low": 67186.62, "close": 67268.4, "volume": 186.97188},
    {"time": 1775336400, "open": 67268.4, "high": 67504.54, "low": 67215, "close": 67359.2, "volume": 238.4254},
    {"time": 1775340000, "open": 67359.21, "high": 67470.93, "low": 67341.19, "close": 67399.92, "volume": 306.3388},
    {"time": 1775343600, "open": 67399.91, "high": 67399.99, "low": 67261.32, "close": 67300.42, "volume": 397.2689},
    {"time": 1775347200, "open": 67300.42, "high": 67307.28, "low": 67168.08, "close": 67225, "volume": 431.19179},
    {"time": 1775350800, "open": 67225, "high": 67230, "low": 67080, "close": 67085, "volume": 369.73604},
    {"time": 1775354400, "open": 67085, "high": 67174.92, "low": 66955.72, "close": 67170.16, "volume": 252.44755},
    {"time": 1775358000, "open": 67170.17, "high": 67188.09, "low": 67089.33, "close": 67145.98, "volume": 163.7387},
    {"time": 1775361600, "open": 67145.98, "high": 67188.51, "low": 67073.56, "close": 67101.65, "volume": 92.89558},
    {"time": 1775365200, "open": 67101.65, "high": 67154.98, "low": 66888, "close": 66931.65, "volume": 466.39227},
    {"time": 1775368800, "open": 66931.65, "high": 66940, "low": 66611.66, "close": 66802.24, "volume": 730.15956},
    {"time": 1775372400, "open": 66802.24, "high": 66914.48, "low": 66715.43, "close": 66820.64, "volume": 615.82267},
    {"time": 1775376000, "open": 66820.64, "high": 66940, "low": 66819.42, "close": 66933.18, "volume": 227.0764},
    {"time": 1775379600, "open": 66933.18, "high": 67048.64, "low": 66902.15, "close": 67035.15, "volume": 296.9621},
    {"time": 1775383200, "open": 67035.15, "high": 67150, "low": 66968.35, "close": 67046.01, "volume": 288.35183},
    {"time": 1775386800, "open": 67046.01, "high": 67087.22, "low": 66973.67, "close": 66999, "volume": 253.07144},
    {"time": 1775390400, "open": 66998.99, "high": 66999, "low": 66680.57, "close": 66783.58, "volume": 607.98096},
    {"time": 1775394000, "open": 66783.59, "high": 66918.03, "low": 66693.91, "close": 66890.7, "volume": 362.16245},
    {"time": 1775397600, "open": 66890.7, "high": 66963.95, "low": 66820.54, "close": 66918.03, "volume": 297.56816},
    {"time": 1775401200, "open": 66918.03, "high": 67856.16, "low": 66829.02, "close": 67306.64, "volume": 1471.54221},
    {"time": 1775404800, "open": 67306.65, "high": 67410, "low": 67173.4, "close": 67212.62, "volume": 356.98825},
    {"time": 1775408400, "open": 67212.61, "high": 67402, "low": 67187.95, "close": 67373.64, "volume": 168.71047},
    {"time": 1775412000, "open": 67373.64, "high": 67451.78, "low": 67281.38, "close": 67413.78, "volume": 214.65548},
    {"time": 1775415600, "open": 67413.78, "high": 67560.45, "low": 67276.67, "close": 67361.01, "volume": 230.21119},
    {"time": 1775419200, "open": 67361.01, "high": 67705.74, "low": 67332.66, "close": 67658.35, "volume": 338.57386},
    {"time": 1775422800, "open": 67658.35, "high": 67682.42, "low": 67408.07, "close": 67553.43, "volume": 436.79816},
    {"time": 1775426400, "open": 67553.44, "high": 68374.75, "low": 67347.05, "close": 68338.43, "volume": 1205.37091},
    {"time": 1775430000, "open": 68338.43, "high": 69136.2, "low": 68266.13, "close": 69034.18, "volume": 1875.95865},
    {"time": 1775433600, "open": 69034.18, "high": 69588, "low": 69034.18, "close": 69083.78, "volume": 1918.78234},
    {"time": 1775437200, "open": 69083.79, "high": 69139.63, "low": 68787.13, "close": 68810.18, "volume": 994.73828},
    {"time": 1775440800, "open": 68810.17, "high": 69408.39, "low": 68776.61, "close": 69207.99, "volume": 952.20286},
    {"time": 1775444400, "open": 69207.99, "high": 69259.06, "low": 69056.86, "close": 69123.69, "volume": 642.40612},
    {"time": 1775448000, "open": 69123.69, "high": 69217.68, "low": 68980.36, "close": 69141.8, "volume": 640.33105},
    {"time": 1775451600, "open": 69141.81, "high": 69247, "low": 69086, "close": 69199.31, "volume": 664.95087},
    {"time": 1775455200, "open": 69199.31, "high": 69354.26, "low": 68806.53, "close": 68976.67, "volume": 805.52815},
    {"time": 1775458800, "open": 68976.67, "high": 69215.37, "low": 68821.07, "close": 69115.91, "volume": 706.16999},
    {"time": 1775462400, "open": 69115.91, "high": 69347.85, "low": 69081.88, "close": 69224.63, "volume": 515.66672},
    {"time": 1775466000, "open": 69224.63, "high": 70283.32, "low": 69214.46, "close": 69728, "volume": 2396.21658},
    {"time": 1775469600, "open": 69728.01, "high": 69950, "low": 69708.13, "close": 69770.88, "volume": 635.27783},
    {"time": 1775473200, "open": 69770.88, "high": 69770.96, "low": 69565.01, "close": 69614.91, "volume": 453.93314},
    {"time": 1775476800, "open": 69614.91, "high": 69635.87, "low": 69329.23, "close": 69366.75, "volume": 601.88109},
    {"time": 1775480400, "open": 69366.76, "high": 69550, "low": 69235.09, "close": 69428.23, "volume": 658.07588},
    {"time": 1775484000, "open": 69428.23, "high": 69809.99, "low": 69163.86, "close": 69654.38, "volume": 1337.06434},
    {"time": 1775487600, "open": 69654.39, "high": 69973.25, "low": 69526.87, "close": 69968.87, "volume": 814.26276},
    {"time": 1775491200, "open": 69968.87, "high": 70351.46, "low": 69782.11, "close": 69799.14, "volume": 1292.09208},
    {"time": 1775494800, "open": 69799.14, "high": 69820, "low": 69282.02, "close": 69574.61, "volume": 916.84733},
    {"time": 1775498400, "open": 69574.62, "high": 69967.78, "low": 69479.49, "close": 69888.97, "volume": 537.96454},
    {"time": 1775502000, "open": 69888.97, "high": 69899.99, "low": 69583.33, "close": 69739.53, "volume": 431.95702},
    {"time": 1775505600, "open": 69739.52, "high": 69974.08, "low": 69625, "close": 69826.01, "volume": 373.86049},
    {"time": 1775509200, "open": 69826, "high": 69826, "low": 69423.09, "close": 69504.99, "volume": 367.56749},
    {"time": 1775512800, "open": 69504.99, "high": 69581.92, "low": 68811, "close": 68811, "volume": 579.09912},
    {"time": 1775516400, "open": 68811.01, "high": 68912.97, "low": 68300, "close": 68853.66, "volume": 1065.57632},
    {"time": 1775520000, "open": 68853.66, "high": 69128.2, "low": 68723.81, "close": 68775.7, "volume": 908.24911},
    {"time": 1775523600, "open": 68775.71, "high": 68900.63, "low": 68352.52, "close": 68400.01, "volume": 682.45962},
    {"time": 1775527200, "open": 68400.01, "high": 68713.92, "low": 68273.34, "close": 68674, "volume": 382.62751},
    {"time": 1775530800, "open": 68674.01, "high": 68800.94, "low": 68625.59, "close": 68779.21, "volume": 449.8892},
    {"time": 1775534400, "open": 68779.2, "high": 68981.83, "low": 68540.84, "close": 68854.65, "volume": 480.46438},
    {"time": 1775538000, "open": 68854.65, "high": 68866, "low": 68613.2, "close": 68669, "volume": 546.72923},
    {"time": 1775541600, "open": 68669, "high": 68669.01, "low": 68443, "close": 68568.35, "volume": 489.56798},
    {"time": 1775545200, "open": 68568.35, "high": 68703.72, "low": 68500, "close": 68624.61, "volume": 307.84134},
    {"time": 1775548800, "open": 68624.6, "high": 68984.48, "low": 68603.6, "close": 68936.71, "volume": 338.98534},
    {"time": 1775552400, "open": 68936.71, "high": 69247.9, "low": 68930.04, "close": 69150.05, "volume": 437.37301},
    {"time": 1775556000, "open": 69150.06, "high": 69199.87, "low": 68200, "close": 68237.93, "volume": 898.85914},
    {"time": 1775559600, "open": 68237.93, "high": 68455.96, "low": 68071.96, "close": 68345.75, "volume": 598.71022},
    {"time": 1775563200, "open": 68345.75, "high": 68599.13, "low": 68080, "close": 68392.18, "volume": 738.67705},
    {"time": 1775566800, "open": 68392.19, "high": 68633.87, "low": 68198.12, "close": 68231.99, "volume": 822.0611},
    {"time": 1775570400, "open": 68231.99, "high": 68258.26, "low": 67763.64, "close": 67860.48, "volume": 1396.10082},
    {"time": 1775574000, "open": 67860.49, "high": 68268.6, "low": 67732.01, "close": 68168.1, "volume": 1084.45539},
    {"time": 1775577600, "open": 68168.11, "high": 68454.23, "low": 68085.01, "close": 68229.99, "volume": 654.26405},
    {"time": 1775581200, "open": 68230, "high": 69000, "low": 68198.22, "close": 68705.81, "volume": 1147.70463},
    {"time": 1775584800, "open": 68705.82, "high": 68773.24, "low": 68358.1, "close": 68442.53, "volume": 630.41269},
    {"time": 1775588400, "open": 68442.52, "high": 69098.44, "low": 68358.1, "close": 69011.29, "volume": 1385.03929},
    {"time": 1775592000, "open": 69011.29, "high": 69535.26, "low": 69011.29, "close": 69319.39, "volume": 1097.55478},
    {"time": 1775595600, "open": 69319.39, "high": 70216.68, "low": 69311.84, "close": 69927.64, "volume": 1742.03489},
    {"time": 1775599200, "open": 69927.65, "high": 71528, "low": 69927.65, "close": 71514.09, "volume": 2841.66596},
    {"time": 1775602800, "open": 71514.1, "high": 72761, "low": 71194.9, "close": 71924.22, "volume": 3690.98922},
    {"time": 1775606400, "open": 71924.22, "high": 72110.65, "low": 71541.51, "close": 71710.19, "volume": 1153.45069},
    {"time": 1775610000, "open": 71710.19, "high": 71758.6, "low": 71452.68, "close": 71476.97, "volume": 621.6206},
    {"time": 1775613600, "open": 71476.98, "high": 71727.01, "low": 71240.99, "close": 71364.94, "volume": 1153.76247},
    {"time": 1775617200, "open": 71364.95, "high": 71479.97, "low": 71277.18, "close": 71287.39, "volume": 441.23135},
    {"time": 1775620800, "open": 71287.39, "high": 71676.77, "low": 71287.38, "close": 71566.18, "volume": 721.11279},
    {"time": 1775624400, "open": 71566.66, "high": 71831.6, "low": 71560, "close": 71731.1, "volume": 821.79755},
    {"time": 1775628000, "open": 71731.1, "high": 71941.42, "low": 71581.14, "close": 71732.69, "volume": 811.19618},
    {"time": 1775631600, "open": 71732.7, "high": 71902, "low": 71573.04, "close": 71627.59, "volume": 943.84918},
    {"time": 1775635200, "open": 71627.59, "high": 71831.08, "low": 71627.59, "close": 71800, "volume": 501.15204},
    {"time": 1775638800, "open": 71799.99, "high": 71955, "low": 71588.75, "close": 71678.87, "volume": 419.9995},
    {"time": 1775642400, "open": 71678.87, "high": 71790, "low": 71457.61, "close": 71476.98, "volume": 514.219},
    {"time": 1775646000, "open": 71476.97, "high": 71739.67, "low": 71407.77, "close": 71675.12, "volume": 779.27898},
    {"time": 1775649600, "open": 71675.12, "high": 72181.81, "low": 71581.76, "close": 72071.95, "volume": 1226.00544},
    {"time": 1775653200, "open": 72071.95, "high": 72857, "low": 71510, "close": 71600.01, "volume": 2821.31683},
    {"time": 1775656800, "open": 71600, "high": 71751.42, "low": 70707.23, "close": 70912.88, "volume": 1895.54834},
    {"time": 1775660400, "open": 70912.89, "high": 71349.05, "low": 70833.3, "close": 71310, "volume": 873.16856},
    {"time": 1775664000, "open": 71309.99, "high": 71699, "low": 71250.93, "close": 71602.1, "volume": 757.94566},
    {"time": 1775667600, "open": 71602.11, "high": 71963.94, "low": 71490.55, "close": 71888.19, "volume": 682.09647},
    {"time": 1775671200, "open": 71888.19, "high": 71888.2, "low": 71085.22, "close": 71099.63, "volume": 703.75724},
    {"time": 1775674800, "open": 71099.63, "high": 71372.22, "low": 71019, "close": 71318.48, "volume": 473.49397},
    {"time": 1775678400, "open": 71318.47, "high": 71481.7, "low": 71267.91, "close": 71372.93, "volume": 226.24698},
    {"time": 1775682000, "open": 71372.94, "high": 71748, "low": 71330.86, "close": 71551.39, "volume": 367.89907},
    {"time": 1775685600, "open": 71551.4, "high": 71571.6, "low": 71064.84, "close": 71064.84, "volume": 374.67564},
    {"time": 1775689200, "open": 71064.84, "high": 71204, "low": 70901.89, "close": 71069.93, "volume": 517.86174},
    {"time": 1775692800, "open": 71069.93, "high": 71133.71, "low": 70650, "close": 70815.5, "volume": 677.85142},
    {"time": 1775696400, "open": 70815.5, "high": 70956.7, "low": 70466, "close": 70928.89, "volume": 495.03665},
    {"time": 1775700000, "open": 70928.9, "high": 71198.8, "low": 70816.36, "close": 71060.64, "volume": 664.15391},
    {"time": 1775703600, "open": 71060.64, "high": 71124.05, "low": 70845.27, "close": 70992.39, "volume": 808.25356},
    {"time": 1775707200, "open": 70992.37, "high": 71006.13, "low": 70734.77, "close": 70782, "volume": 304.15042},
    {"time": 1775710800, "open": 70781.99, "high": 71087.12, "low": 70689.38, "close": 70984, "volume": 495.401},
    {"time": 1775714400, "open": 70983.99, "high": 71082.71, "low": 70809.94, "close": 71031.84, "volume": 538.94746},
    {"time": 1775718000, "open": 71031.83, "high": 71116, "low": 70955.4, "close": 70982.01, "volume": 922.88611},
    {"time": 1775721600, "open": 70982.01, "high": 71380, "low": 70865.42, "close": 71249.51, "volume": 562.42694},
    {"time": 1775725200, "open": 71249.51, "high": 71575, "low": 71109.99, "close": 71534.68, "volume": 512.0201},
    {"time": 1775728800, "open": 71534.69, "high": 71534.69, "low": 71276.44, "close": 71276.45, "volume": 304.66693},
    {"time": 1775732400, "open": 71276.45, "high": 71350, "low": 71038.5, "close": 71150.17, "volume": 446.85377},
    {"time": 1775736000, "open": 71150.17, "high": 71328.79, "low": 71040, "close": 71279.63, "volume": 382.04593},
    {"time": 1775739600, "open": 71279.63, "high": 71355.24, "low": 70629.3, "close": 70801.98, "volume": 696.59545},
    {"time": 1775743200, "open": 70801.97, "high": 71250.39, "low": 70522.77, "close": 71123.71, "volume": 873.72323},
    {"time": 1775746800, "open": 71123.71, "high": 72358, "low": 70929.44, "close": 72141.99, "volume": 2048.75279},
    {"time": 1775750400, "open": 72142, "high": 72399, "low": 71729.66, "close": 72080.87, "volume": 1429.6831},
    {"time": 1775754000, "open": 72080.86, "high": 72550, "low": 72062.38, "close": 72347.49, "volume": 1415.90923},
    {"time": 1775757600, "open": 72348, "high": 72380.7, "low": 71747.68, "close": 71888.69, "volume": 560.32535},
    {"time": 1775761200, "open": 71888.69, "high": 72300, "low": 71750, "close": 72096.32, "volume": 607.89039},
    {"time": 1775764800, "open": 72096.33, "high": 72458.31, "low": 71985.95, "close": 72413.97, "volume": 497.89389},
    {"time": 1775768400, "open": 72413.96, "high": 72700, "low": 72142.79, "close": 72264.95, "volume": 771.79321},
    {"time": 1775772000, "open": 72264.96, "high": 73145, "low": 71945.94, "close": 71946.01, "volume": 1345.32358},
    {"time": 1775775600, "open": 71946.01, "high": 72025.53, "low": 71585.25, "close": 71787.97, "volume": 795.83731},
    {"time": 1775779200, "open": 71787.98, "high": 72028.4, "low": 71573.35, "close": 71963.03, "volume": 967.9743},
    {"time": 1775782800, "open": 71963.04, "high": 72387, "low": 71673.48, "close": 72157.93, "volume": 679.67117},
    {"time": 1775786400, "open": 72157.93, "high": 72291.04, "low": 71921.59, "close": 71965.36, "volume": 515.54191},
    {"time": 1775790000, "open": 71965.36, "high": 72027.37, "low": 71813.73, "close": 71868.67, "volume": 543.68963},
    {"time": 1775793600, "open": 71868.67, "high": 72212.84, "low": 71820.47, "close": 72117.29, "volume": 734.17151},
    {"time": 1775797200, "open": 72117.29, "high": 72267.89, "low": 72010.4, "close": 72127.84, "volume": 408.69549},
    {"time": 1775800800, "open": 72127.84, "high": 72179.85, "low": 71792.01, "close": 71805.38, "volume": 448.00931},
    {"time": 1775804400, "open": 71805.39, "high": 71837.59, "low": 71428, "close": 71481.85, "volume": 702.39043},
    {"time": 1775808000, "open": 71481.85, "high": 71769.41, "low": 71426.15, "close": 71606.07, "volume": 624.89096},
    {"time": 1775811600, "open": 71606.08, "high": 71873, "low": 71583.47, "close": 71787.41, "volume": 281.02303},
    {"time": 1775815200, "open": 71787.41, "high": 71928.29, "low": 71711.6, "close": 71910.09, "volume": 238.63388},
    {"time": 1775818800, "open": 71910.1, "high": 72261.93, "low": 71903.69, "close": 72127.59, "volume": 421.17579},
    {"time": 1775822400, "open": 72127.59, "high": 72458, "low": 72007.5, "close": 72258.68, "volume": 733.4245},
    {"time": 1775826000, "open": 72258.69, "high": 72409.41, "low": 71899.51, "close": 72321.4, "volume": 673.2668},
    {"time": 1775829600, "open": 72321.39, "high": 73155.58, "low": 72258, "close": 72909, "volume": 2362.6198},
    {"time": 1775833200, "open": 72909, "high": 73289.96, "low": 72355.87, "close": 72460.51, "volume": 1324.05053},
    {"time": 1775836800, "open": 72460.52, "high": 73069.4, "low": 72387.41, "close": 72992.33, "volume": 733.09854},
    {"time": 1775840400, "open": 72992.32, "high": 73146, "low": 72802.96, "close": 73121.66, "volume": 454.50524},
    {"time": 1775844000, "open": 73121.66, "high": 73159.81, "low": 72880.95, "close": 73107.07, "volume": 597.01721},
    {"time": 1775847600, "open": 73107.07, "high": 73264.76, "low": 72900, "close": 73236.11, "volume": 713.54118},
    {"time": 1775851200, "open": 73236.11, "high": 73434, "low": 73051.97, "close": 73361.68, "volume": 872.16866},
    {"time": 1775854800, "open": 73361.68, "high": 73364.47, "low": 72934.53, "close": 73190.67, "volume": 654.89865},
    {"time": 1775858400, "open": 73190.68, "high": 73244.03, "low": 72700.6, "close": 72877, "volume": 798.3469},
    {"time": 1775862000, "open": 72877.01, "high": 72967.79, "low": 72791.96, "close": 72962.7, "volume": 889.83693},
    {"time": 1775865600, "open": 72962.71, "high": 72962.71, "low": 72787.94, "close": 72828.42, "volume": 801.54149},
    {"time": 1775869200, "open": 72828.42, "high": 72999.99, "low": 72670, "close": 72954.39, "volume": 320.3002},
    {"time": 1775872800, "open": 72954.39, "high": 73094.89, "low": 72910.93, "close": 72966.57, "volume": 288.43552},
    {"time": 1775876400, "open": 72966.58, "high": 73075.34, "low": 72814.42, "close": 72866.63, "volume": 227.12944},
    {"time": 1775880000, "open": 72866.62, "high": 72954.28, "low": 72681.9, "close": 72806.08, "volume": 298.14959},
    {"time": 1775883600, "open": 72806.08, "high": 72836.55, "low": 72679.08, "close": 72722.28, "volume": 222.17067},
    {"time": 1775887200, "open": 72722.28, "high": 72771.32, "low": 72612.16, "close": 72762, "volume": 590.44339},
    {"time": 1775890800, "open": 72762.01, "high": 72832, "low": 72672.56, "close": 72745.38, "volume": 321.45905},
    {"time": 1775894400, "open": 72745.38, "high": 72799.57, "low": 72638.44, "close": 72664.17, "volume": 263.67236},
    {"time": 1775898000, "open": 72664.16, "high": 72908, "low": 72617.75, "close": 72907.11, "volume": 306.96352},
    {"time": 1775901600, "open": 72907.12, "high": 72915.95, "low": 72760.9, "close": 72843.71, "volume": 224.83128},
    {"time": 1775905200, "open": 72843.7, "high": 72912, "low": 72826.37, "close": 72907.05, "volume": 109.63598},
    {"time": 1775908800, "open": 72907.05, "high": 72982.4, "low": 72814.97, "close": 72940.49, "volume": 180.21756},
    {"time": 1775912400, "open": 72940.5, "high": 72940.5, "low": 72513.09, "close": 72658.07, "volume": 493.97275},
    {"time": 1775916000, "open": 72658.07, "high": 72770.91, "low": 72609.27, "close": 72704.67, "volume": 212.83973},
    {"time": 1775919600, "open": 72704.66, "high": 72896.03, "low": 72678, "close": 72858.17, "volume": 246.74388},
    {"time": 1775923200, "open": 72858.18, "high": 73204.74, "low": 72812.94, "close": 73034.33, "volume": 656.04744},
    {"time": 1775926800, "open": 73034.34, "high": 73169.04, "low": 72942.06, "close": 73095.97, "volume": 269.56328},
    {"time": 1775930400, "open": 73095.97, "high": 73726.13, "low": 73032.61, "close": 73547.89, "volume": 856.65701},
    {"time": 1775934000, "open": 73547.9, "high": 73790, "low": 73472.38, "close": 73687.15, "volume": 595.037},
    {"time": 1775937600, "open": 73687.16, "high": 73689.97, "low": 73226.28, "close": 73290.43, "volume": 395.49217},
    {"time": 1775941200, "open": 73290.43, "high": 73548.08, "low": 73200.48, "close": 73427.04, "volume": 366.94025},
    {"time": 1775944800, "open": 73427.04, "high": 73574, "low": 73234.25, "close": 73376.01, "volume": 431.15797},
    {"time": 1775948400, "open": 73376.01, "high": 73376.01, "low": 72907.4, "close": 73043.16, "volume": 391.57799},
    {"time": 1775952000, "open": 73043.16, "high": 73117.32, "low": 72873.25, "close": 73079.96, "volume": 390.45862},
    {"time": 1775955600, "open": 73079.96, "high": 73137.24, "low": 71310, "close": 71643.69, "volume": 2072.10202},
    {"time": 1775959200, "open": 71643.69, "high": 71982.46, "low": 71352.47, "close": 71761.35, "volume": 839.35622},
    {"time": 1775962800, "open": 71761.35, "high": 71861.33, "low": 71593.18, "close": 71593.19, "volume": 790.20901},
    {"time": 1775966400, "open": 71593.18, "high": 71685.31, "low": 71444.68, "close": 71466.53, "volume": 611.50691},
    {"time": 1775970000, "open": 71466.53, "high": 71781, "low": 71417.22, "close": 71710.2, "volume": 667.76838},
    {"time": 1775973600, "open": 71710.19, "high": 71753.33, "low": 71633.77, "close": 71668.01, "volume": 377.10612},
    {"time": 1775977200, "open": 71668.01, "high": 71747.31, "low": 71588.57, "close": 71665, "volume": 313.33908},
    {"time": 1775980800, "open": 71664.99, "high": 71805.85, "low": 71595, "close": 71629.21, "volume": 922.36362},
    {"time": 1775984400, "open": 71629.21, "high": 71633.01, "low": 71488.45, "close": 71531.94, "volume": 372.12981},
    {"time": 1775988000, "open": 71531.95, "high": 71682.72, "low": 71357.76, "close": 71452.89, "volume": 274.78665},
    {"time": 1775991600, "open": 71452.89, "high": 71556.12, "low": 71412.57, "close": 71468.55, "volume": 211.53603},
    {"time": 1775995200, "open": 71468.54, "high": 71500, "low": 71077.44, "close": 71084.01, "volume": 608.12473},
    {"time": 1775998800, "open": 71084.01, "high": 71146.03, "low": 70866.82, "close": 70889.42, "volume": 708.82478},
    {"time": 1776002400, "open": 70889.42, "high": 71092.78, "low": 70700, "close": 70748.34, "volume": 541.94034},
    {"time": 1776006000, "open": 70748.34, "high": 70932, "low": 70604.07, "close": 70892.03, "volume": 502.43194},
    {"time": 1776009600, "open": 70892.04, "high": 70959.51, "low": 70821.05, "close": 70936.27, "volume": 259.94883},
    {"time": 1776013200, "open": 70936.27, "high": 71020.01, "low": 70900, "close": 70975.28, "volume": 200.45317},
]

# Indicator values captured at same moment as OHLCV (first capture)
# Note: Both EMAs showed same value (68,330.22) — TV didn't apply length=21 to second EMA.
# The EMA value likely corresponds to EMA(9) since that was added first.
# RSI, BBands, MACD, ATR are all reliable.
# Third capture — with EMA_21 properly configured.
# All values aligned with OHLCV last bar (row 199, still forming).
# EMA_21 confirmed via data_get_indicator: entity FUPFqt, length=21.
# EMA_9 confirmed via data_get_indicator: entity lPRtfh, length=9.
# ADX_14 and STOCHk_14 on chart but data_get_study_values doesn't read
# separate-pane indicators — verified by hand-calculated tests in Phase 2.
# Reference bar: time=1776006000 (Sun 12 Apr 2026 20:30 IST / 15:00 UTC)
# This is a CLOSED historical bar (index 197 of 200, aka iloc[-3]).
# Pinned values manually read from TradingView Data Window — one indicator at a time,
# screenshot verified by Prince on 2026-04-12.
# Closed bars are immutable, so these golden values never drift.
REFERENCE_BAR_TIMESTAMP_MS = 1776006000 * 1000  # Sun 12 Apr 2026 15:00 UTC
REFERENCE_BAR_INDEX = 197  # 0-indexed, 3rd-from-last in 200-bar fixture

INDICATORS = {
    # EMA 9 — bit-exact (tested)
    "EMA_9": 71217.74,
    # RSI 14 — bit-exact
    "RSI_14": 28.74,
    # Bollinger Bands (length=20, std=2) — all 3 bit-exact
    "BBU_20": 73559.98,
    "BBM_20": 71871.05,
    "BBL_20": 70182.12,
    # MACD (12, 26, 9) — bit-exact on all outputs
    "MACD": -481.01,
    "MACD_signal": -379.70,
    "MACD_hist": -101.31,
    # ATR 14 (RMA smoothing = Wilder's) — bit-exact
    "ATR_14": 341.59,
    # Directional Movement (length=14, adx_smoothing=14) — bit-exact
    "ADX_14": 40.4512,
    "DI+_14": 8.5340,
    "DI-_14": 37.6365,
    # Stochastic (14, 3, 3) — TV's %K with default smoothing=3 corresponds to
    # our STOCHd_14 (ta.stoch_signal = SMA3 of raw %K). Bit-exact at this mapping.
    # Our STOCHk_14 = raw %K (TV smoothing=1) — not verified here, covered by Phase 2 hand calc.
    "STOCHd_14": 7.96,
}


def main():
    # Convert bar format for our test fixture
    ohlcv = []
    for bar in BARS:
        ohlcv.append({
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "close": bar["close"],
            "volume": bar["volume"],
            "timestamp": bar["time"] * 1000,  # Convert to ms
        })

    fixture = {
        "captured_at": "2026-04-12",
        "symbol": "BTCUSDT",
        "timeframe": "1H",
        "exchange": "BINANCE",
        "bar_count": len(ohlcv),
        "reference_bar_timestamp_ms": REFERENCE_BAR_TIMESTAMP_MS,
        "reference_bar_index": REFERENCE_BAR_INDEX,
        "note": (
            "Indicator values pinned to a CLOSED historical bar "
            "(Sun 12 Apr 2026 20:30 IST / 15:00 UTC, index 197). "
            "Values manually verified one-by-one in TradingView Data Window, "
            "each indicator loaded alone to avoid legend overlap. "
            "Bit-exact to 4 decimals for all verified indicators."
        ),
        "ohlcv": ohlcv,
        "indicators": INDICATORS,
    }

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FIXTURE_PATH, "w") as f:
        json.dump(fixture, f, indent=2)

    print(f"Fixture saved to {FIXTURE_PATH}")
    print(f"  Bars: {len(ohlcv)}")
    print(f"  Indicators: {list(INDICATORS.keys())}")


if __name__ == "__main__":
    main()
