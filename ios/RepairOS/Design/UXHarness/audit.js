// Execute this entire file as the code argument to Figma MCP use_figma.
// Read-only except for changing the current page. No service simulations.
await figma.setCurrentPageAsync(await figma.getNodeByIdAsync('2:3'));
const board = await figma.getNodeByIdAsync('86:157');
const results = [];
function check(name, pass, detail) {
  results.push({ name, pass: Boolean(pass), detail });
}
const visible = n => n.visible && (!n.parent || n.parent.type === 'PAGE' || visible(n.parent));
const variables = new Map((await figma.variables.getLocalVariablesAsync()).map(v => [v.id, v]));
const glassStyle = (await figma.getLocalEffectStylesAsync()).find(s => s.name === 'Material/Frosted glass');
function solid(paint, node) {
  const id = paint.boundVariables?.color?.id;
  const value = id ? variables.get(id).resolveForConsumer(node).value : paint.color;
  return { r: value.r, g: value.g, b: value.b, a: value.a ?? paint.opacity ?? 1 };
}
function over(fg, bg) {
  return { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 };
}
function background(node) {
  if (!node || node.type === 'PAGE') return { r: 1, g: 1, b: 1, a: 1 };
  let bg = background(node.parent);
  if ('fills' in node && Array.isArray(node.fills)) {
    for (const p of [...node.fills].reverse()) if (p.visible !== false && p.type === 'SOLID') bg = over(solid(p, node), bg);
  }
  return bg;
}
function luminance(c) {
  const linear = x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4;
  return .2126 * linear(c.r) + .7152 * linear(c.g) + .0722 * linear(c.b);
}
function contrast(a, b) {
  const x = luminance(a), y = luminance(b);
  return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
}
check('Regression board exists', board?.type === 'FRAME', board?.id);
if (!board) return { passed: false, results };
const cases = board.children.filter(n => n.name.endsWith(' cases')).flatMap(n => n.children);
check('Six width / type-size / appearance cases', cases.length === 6, cases.map(n => n.name));
// Check semantic pairs as well as the specimen text actually rendered below.
for (const [mode, consumer] of [['Light', cases[0]], ['Dark', cases[3]]]) {
  const value = name => [...variables.values()].find(v => v.name === name).resolveForConsumer(consumer).value;
  for (const fg of ['ink', 'secondary', 'accent', 'tone/cosmetic', 'tone/danger', 'tone/caution', 'tone/good']) {
    for (const bg of ['canvas', 'surface']) {
      const ratio = contrast(value(fg), value(bg));
      check(`${mode}: semantic text contrast ${fg} on ${bg}`, ratio >= 4.5, Number(ratio.toFixed(2)));
    }
  }
  const ratio = contrast(value('onAccent'), value('accent'));
  check(`${mode}: semantic text contrast onAccent on accent`, ratio >= 4.5, Number(ratio.toFixed(2)));
}
const descendantCounts = {};
for (const n of board.findAll()) descendantCounts[n.type] = (descendantCounts[n.type] || 0) + 1;
for (const c of cases) {
  const instances = c.children.filter(n => n.type === 'INSTANCE');
  check(`${c.name}: eight linked components`, instances.length === 8, instances.length);
  for (const i of instances) {
    const main = await i.getMainComponentAsync();
    check(`${c.name}: ${i.name} remains linked`, Boolean(main), main?.id);
  }
  const warning = instances.find(n => n.name === 'Safety Card');
  const glass = instances.find(n => n.name === 'Glass control');
  check(`${c.name}: glass material is linked`, glass && glass.effectStyleId === glassStyle?.id, glass?.effectStyleId);
  check(`${c.name}: glass has background frost`, glass?.effects.some(e => e.type === 'BACKGROUND_BLUR' && e.radius === 24), glass?.effects);
  check(`${c.name}: warning width`, Math.abs(warning.width - Number(c.name.split(' / ')[1].split(' ')[0])) < 1, warning.width);
  for (const t of c.findAllWithCriteria({ types: ['TEXT'] }).filter(visible)) {
    const r = t.absoluteBoundingBox, p = t.parent.absoluteBoundingBox;
    check(`${c.name}: text containment ${t.id}`, r && p && r.x >= p.x - 1 && r.y >= p.y - 1 && r.x + r.width <= p.x + p.width + 1 && r.y + r.height <= p.y + p.height + 1, { text: t.characters, bounds: r, parent: p });
    check(`${c.name}: product reading font ${t.id}`, t.fontName.family === 'Inter', t.fontName);
    check(`${c.name}: no false link styling ${t.id}`, t.textDecoration === 'NONE', t.textDecoration);
    const paints = Array.isArray(t.fills) ? t.fills.filter(p => p.visible !== false) : [];
    check(`${c.name}: measurable solid text ${t.id}`, paints.length === 1 && paints[0].type === 'SOLID', paints.length);
    if (paints.length === 1 && paints[0].type === 'SOLID') {
      const bg = background(t.parent), ratio = contrast(over(solid(paints[0], t), bg), bg);
      check(`${c.name}: text contrast >= 4.5 ${t.id}`, ratio >= 4.5, Number(ratio.toFixed(2)));
    }
  }
}
const tags = await figma.getNodeByIdAsync('3:38');
check('Six semantic tag variants retained', tags.children.length === 6, tags.children.length);
for (const tag of tags.children) {
  const unknown = tag.name === 'Tone=Unknown';
  check(`${tag.name}: read-only, no button reactions`, tag.reactions.length === 0, tag.reactions.length);
  check(`${tag.name}: outline reserved for unknown`, unknown ? tag.strokes.length > 0 && tag.dashPattern.length > 0 : tag.strokes.length === 0, tag.dashPattern);
  const label = tag.findOne(n => n.type === 'TEXT');
  if (unknown) check('Unknown never presented as success', label.characters === 'Not inspected', label.characters);
}
for (const n of board.findAll()) {
  if ('effects' in n) {
    check(`Effects restricted to glass controls ${n.id}`, n.effects.length === 0 || (n.name === 'Glass control' && n.effectStyleId === glassStyle?.id), n.effects.map(e => e.type));
    check(`No layer blur or noise ${n.id}`, n.effects.every(e => ['BACKGROUND_BLUR', 'INNER_SHADOW', 'DROP_SHADOW'].includes(e.type)), n.effects.map(e => e.type));
  }
  if ('fills' in n && Array.isArray(n.fills)) check(`Editable content ${n.id}`, !n.fills.some(f => f.type === 'IMAGE'), n.type);
}
const failures = results.filter(r => !r.pass);
return {
  passed: failures.length === 0,
  checks: results.length,
  failures,
  minTextContrast: Math.min(...results.filter(r => r.name.includes('text contrast')).map(r => r.detail)),
  descendantCounts,
  limits: ['Figma only: no VoiceOver, physical touch-target or user task-completion validation.', '200% font specimens test reflow, not native Dynamic Type behavior.', 'Motion guidance is specified on the board; no application animation was implemented.'],
  passedChecks: results.filter(r => r.pass).length
};
