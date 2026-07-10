export const NODE_COLORS = {
  Document: '#4fc3f7',
  Entity: '#ffb74d',
  Category: '#81c784',
  'AI模型': '#e57373',
  'API协议': '#ba68c8',
  SDK: '#4db6ac',
  工具: '#ff8a65',
  框架: '#a1887f',
  服务: '#90a4ae',
  协议: '#7986cb',
  应用: '#f06292',
  组件: '#4dd0e1',
  概念: '#fff176',
}

export function getNodeColor(type) {
  return NODE_COLORS[type] || '#90a4ae'
}
