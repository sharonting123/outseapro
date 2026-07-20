"""竞价 / 计费口径说明（对齐真实 OCPX 分层）。

## 1. 估值核（数学成立）

对转化优化（OCPC / OCPM）：

    value_per_imp = pCTR × pCVR × bid
                  = pCVR_view × bid

    bid          : 广告主设置的「单次转化目标出价」
    pCTR         : 预估点击率
    pCVR         : 预估点击后转化率
    pCVR_view    : 单次展现预估转化概率 = pCTR × pCVR

这是「这条曝光能带来的预估转化总价值」，千次口径即粗 eCPM。
它只是估值，不是完整竞价规则。

## 2. 排序 eCPM（必须乘质量分 Q）

    rank_eCPM = pCTR × pCVR × bid × Q × 1000

Q 来自素材质量、账户转化稳定性、负反馈惩罚。低质素材即使抬高 bid
也会被 Q 压住，无法简单「花钱砸排名」。

代码：`ranker.quality_factor` + `Ranker.rank_value`

## 3. 扣费 ≠ bid（二价清算）

OCPC（按点击计费、优化转化，最主流 OCPX）：
    - 用 rank_eCPM 抢曝光
    - 用户点击后才扣费
    - charge_cpc = next.rank_eCPM / (own.pCTR × own.pCVR × own.Q × 1000) + ε
    - 长期平均转化成本被调控趋近 bid，单次成本会浮动

口语「下一名预估转化价值 ÷ 自己的 pCVR」是不完整 shorthand；
完整 GSP 分母是赢家排序密度 pCTR×pCVR×Q。

OCPM（按千次曝光计费、优化转化）：
    charge_cpm = next.rank_eCPM / own.Q + ε

代码：`auction._clearing_price`

## 4. 命名边界

| 类型 | 优化目标 | 计费 | 排序估值核 |
|------|----------|------|------------|
| CPC  | 无（手动） | 点击 | pCTR × bid × Q |
| CPM  | 无（手动） | 千次曝光 | bid × Q |
| OCPC | 转化 | 点击 | pCTR × pCVR × bid × Q |
| OCPM | 转化 | 千次曝光 | pCTR × pCVR × bid × Q |

「OCPX」= 转化优化统称；本仓库显式拆成 OCPC / OCPM。

## 5. oCPX 自动调价（成本调控）

广告主设的是目标转化出价 `target_bid`（= `ad.bid`）。
平台不会直接按这个数字扣费，而是用反馈回路调节**排序用有效出价**：

    actual_cpa = spend / conversions
    bid_multiplier ≈ clip(target_cpa / actual_cpa, 0.5, 2.0)
    effective_bid = target_bid × bid_multiplier
    rank_eCPM = pCTR × pCVR × effective_bid × Q × 1000

- 实际 CPA 偏高 → multiplier < 1 → 少拿高价流量
- 实际 CPA 偏低 → multiplier > 1 → 多拿量
- 扣费仍走 GSP；调价只改排序强度，间接让长期 CPA 趋近目标

代码：`bid_controller.BidController`
"""
