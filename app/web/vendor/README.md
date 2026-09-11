# vendor/

第三方库，**本地随包分发**，不走 CDN。

## echarts.min.js — Apache License 2.0

- 版本：ECharts 6.x（`echarts.min.js` 完整构建，含 custom 系列）
- 许可证原文：`echarts-LICENSE.txt`
- 来源：本机 `nubimetrics-platform/app/node_modules/echarts/dist/`

★ 为什么随包分发而不是 CDN：
  这个系统跑在本机、只监听 127.0.0.1，用户可能在没有外网的环境下开它。
  依赖 CDN 意味着断网时**所有图表静默变白**，而页面其余部分正常 ——
  这种"一半坏了"的状态最难排查。

★ 为什么用 ECharts 而不是手写 SVG：
  需要的是丝滑的过渡动画、坐标轴联动、tooltip、以及响应式重排。
  这些手写要写很久而且很难写好；ECharts 是同一台机器上参考项目
  （nubimetrics）已经验证过的选择，口径也能对齐。
