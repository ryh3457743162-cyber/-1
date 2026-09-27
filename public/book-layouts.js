/* One source of truth for the editor thumbnails and the public book pages. */
window.BOOK_LAYOUTS = Object.freeze({
  single: {label:'1张 · 单图',count:1,areas:['a'],columns:'1fr',rows:'1fr'},
  'two-horizontal': {label:'2张 · 左右',count:2,areas:['a b'],columns:'1fr 1fr',rows:'1fr'},
  'two-vertical': {label:'2张 · 上下',count:2,areas:['a','b'],columns:'1fr',rows:'1fr 1fr'},
  'two-feature': {label:'2张 · 一大一小',count:2,areas:['a b'],columns:'1.5fr 1fr',rows:'1fr'},
  'three-hero-left': {label:'3张 · 左大右二',count:3,areas:['a b','a c'],columns:'1.45fr 1fr',rows:'1fr 1fr'},
  'three-hero-top': {label:'3张 · 上大下二',count:3,areas:['a a','b c'],columns:'1fr 1fr',rows:'1.25fr 1fr'},
  'four-grid': {label:'4张 · 四宫格',count:4,areas:['a b','c d'],columns:'1fr 1fr',rows:'1fr 1fr'},
  'note-left-photo': {label:'上留言 · 下单图',count:1,photoAreas:['b'],noteArea:'a',areas:['a','b'],columns:'1fr',rows:'1fr 1.15fr'},
  'photo-left-note': {label:'上单图 · 下留言',count:1,photoAreas:['a'],noteArea:'b',areas:['a','b'],columns:'1fr',rows:'1.15fr 1fr'},
  'note-left-two': {label:'上留言 · 下双图',count:2,photoAreas:['b','c'],noteArea:'a',areas:['a a','b c'],columns:'1fr 1fr',rows:'1fr 1.15fr'},
  'photo-bottom-note': {label:'单图 · 底部一句',count:1,photoAreas:['a'],noteArea:'b',areas:['a','b'],columns:'1fr',rows:'3fr 1fr'},
  'note-only': {label:'纯文字纪念页',count:0,photoAreas:[],noteArea:'a',areas:['a'],columns:'1fr',rows:'1fr'}
});
window.applyBookGrid = function(element, layoutName) {
  const layout = window.BOOK_LAYOUTS[layoutName] || window.BOOK_LAYOUTS.single;
  element.style.gridTemplateAreas = layout.areas.map(row => '"' + row + '"').join(' ');
  element.style.gridTemplateColumns = layout.columns;
  element.style.gridTemplateRows = layout.rows;
  return layout;
};
