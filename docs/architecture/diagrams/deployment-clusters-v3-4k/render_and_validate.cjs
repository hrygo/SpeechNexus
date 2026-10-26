/** Render both SVGs at native 4K and check spacing in the actual font renderer. */
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const {pathToFileURL} = require('node:url');
const {chromium} = require('playwright');
const sharp = require('sharp');

const root = __dirname;
const names = ['01-cluster-overview-4k', '02-model-instance-topology-4k'];
const channelIndex = process.argv.indexOf('--channel');
const channel = channelIndex < 0 ? undefined : process.argv[channelIndex + 1];
const sha256 = data => crypto.createHash('sha256').update(data).digest('hex');

async function inspect(page, expected) {
  return page.evaluate(expected => {
    const labels = [...document.querySelectorAll('text[data-check-box]')];
    const icons = [...document.querySelectorAll('[data-layout-icon]')];
    const illustrations = [...document.querySelectorAll('image[data-illustration]')];
    const text = labels.map(e => e.textContent).join('\n');
    const box = e => {
      const b = e.getBoundingClientRect();
      return {x:b.x, y:b.y, right:b.right, bottom:b.bottom, w:b.width, h:b.height};
    };
    const gap = (a,b) => Math.hypot(Math.max(a.x-b.right,b.x-a.right,0),
                                  Math.max(a.y-b.bottom,b.y-a.bottom,0));
    const intersection = (a,b) => Math.min(a.right,b.right)-Math.max(a.x,b.x)>1 &&
                                 Math.min(a.bottom,b.bottom)-Math.max(a.y,b.y)>1;
    const overflow=[], outside=[], padding=[], overlaps=[], iconClearance=[],
          textClearance=[], routeText=[], routeImages=[], imageText=[];
    let minimumInset=Infinity;
    for (const el of labels) {
      const b=el.getBBox(), max=Number(el.dataset.maxWidth), label=el.textContent;
      if (max && b.width>max+.75) overflow.push({label,width:b.width,max});
      if (b.x<0 || b.y<0 || b.x+b.width>1920 || b.y+b.height>1080) outside.push(label);
      if (el.dataset.owner) {
        const owner=document.getElementById(el.dataset.owner), p=owner.getBBox();
        const inset=Number(owner.dataset.inset);
        const margins=[b.x-p.x,b.y-p.y,p.x+p.width-b.x-b.width,p.y+p.height-b.y-b.height];
        minimumInset=Math.min(minimumInset,...margins);
        if (Math.min(...margins)<inset-.75) padding.push({label,owner:owner.id,margins,inset});
      }
    }
    for (let i=0;i<labels.length;i++) {
      const a=box(labels[i]);
      for(let j=i+1;j<labels.length;j++) {
        const b=box(labels[j]);
        if(intersection(a,b)) overlaps.push([labels[i].textContent,labels[j].textContent]);
        else if(gap(a,b)<6) textClearance.push([labels[i].textContent,labels[j].textContent,gap(a,b)]);
      }
      for(const icon of icons) {
        const distance=gap(a,box(icon));
        if(distance<8) iconClearance.push({text:labels[i].textContent,icon:icon.dataset.icon,distance});
      }
      for(const illustration of illustrations) {
        if(intersection(a,box(illustration))) imageText.push(labels[i].textContent);
      }
    }
    for(const route of document.querySelectorAll('path[data-route]')) {
      const length=route.getTotalLength();
      for(const el of labels) {
        const b=el.getBBox();
        for(let distance=4;distance<length-4;distance+=2) {
          const p=route.getPointAtLength(distance);
          if(p.x>b.x-3 && p.x<b.x+b.width+3 && p.y>b.y-3 && p.y<b.y+b.height+3) {
            routeText.push({path:route.getAttribute('d'),text:el.textContent}); break;
          }
        }
      }
      for(const illustration of illustrations) {
        const b=illustration.getBBox();
        for(let distance=4;distance<length-4;distance+=2) {
          const p=route.getPointAtLength(distance);
          if(p.x>b.x && p.x<b.x+b.width && p.y>b.y && p.y<b.y+b.height) {
            routeImages.push({path:route.getAttribute('d'),image:illustration.dataset.illustration}); break;
          }
        }
      }
    }
    const missing=expected.expectedText.filter(s=>!text.includes(s));
    const forbidden=text.match(/Gate\b|#\d+|待验收|待设计|已交付|尚未|实施进度|2026-\d\d-\d\d|\uFFFD/gi)||[];
    const root=document.documentElement.getBoundingClientRect();
    return {
      canvas:{width:root.width,height:root.height},
      labels:labels.length, icons:icons.length, illustrations:illustrations.length,
      minimumFontPx:Math.min(...labels.map(e=>Number(e.getAttribute('font-size'))))*2,
      minimumOwnedInsetPx:Number((minimumInset*2).toFixed(2)),
      spacingPolicy:{textGapPx:6,iconGapPx:8,routeTextGapPx:6,containerInset:'per-container data-inset'},
      missing,forbidden,overflow,outside,padding,overlaps,textClearance,iconClearance,
      routeText,routeImages,imageText
    };
  }, expected);
}

(async()=>{
  const browser=await chromium.launch({headless:true,...(channel?{channel}:{})});
  const results=[];
  try {
    for(const name of names) {
      const svg=await fs.readFile(path.join(root,name+'.svg'));
      const expected=JSON.parse(await fs.readFile(path.join(root,name+'.content.json'),'utf8'));
      const page=await browser.newPage({viewport:{width:3840,height:2160},deviceScaleFactor:1});
      const failures=[];
      page.on('requestfailed',request=>failures.push(request.failure()?.errorText||'request failed'));
      await page.goto(pathToFileURL(path.join(root,name+'.svg')).href,{waitUntil:'load'});
      await page.evaluate(()=>document.fonts.ready);
      const layout=await inspect(page,expected);
      const image=await page.locator('#poster').screenshot({path:path.join(root,name+'.png')});
      await sharp(image).resize({width:1920}).png().toFile(path.join(root,name+'.preview.png'));
      const meta=await sharp(image).metadata();
      const checks=['missing','forbidden','overflow','outside','padding','overlaps','textClearance',
                    'iconClearance','routeText','routeImages','imageText'];
      const pass=checks.every(k=>layout[k].length===0) && failures.length===0 &&
                 layout.labels===expected.textLabels && layout.icons===expected.iconCount &&
                 meta.width===3840 && meta.height===2160 && layout.minimumFontPx>=22;
      const result={status:pass?'passed':'failed',file:name+'.png',svgSha256:sha256(svg),
        pngSha256:sha256(image),pixels:{width:meta.width,height:meta.height},layout,resourceFailures:failures};
      await fs.writeFile(path.join(root,name+'.check.json'),JSON.stringify(result,null,2)+'\n');
      results.push(result);
      console.log(JSON.stringify({file:name,status:result.status,labels:layout.labels,
        findings:Object.fromEntries(checks.filter(k=>layout[k].length).map(k=>[k,layout[k]]))}));
      await page.close();
    }
  } finally {await browser.close();}
  const assets=[];
  for(const name of ['application','compute','data']) {
    const data=await fs.readFile(path.join(root,'assets',name+'.png'));
    const meta=await sharp(data).metadata();
    assets.push({file:'assets/'+name+'.png',width:meta.width,height:meta.height,
                 hasAlpha:meta.hasAlpha,sha256:sha256(data)});
  }
  const catalog=JSON.parse(await fs.readFile(path.join(root,'model-catalog.json'),'utf8'));
  const catalogPass=catalog.length===8 && new Set(catalog.map(x=>x.alias)).size===8 &&
    catalog.filter(x=>x.engine==='vLLM').length===2 &&
    catalog.filter(x=>x.engine==='vLLM-Omni').length===3 &&
    ['qwen_asr','NeMo','ONNX Runtime'].every(engine=>catalog.some(x=>x.engine===engine));
  const validation={status:results.every(x=>x.status==='passed') && catalogPass &&
    assets.every(x=>x.hasAlpha)?'passed':'failed',verifiedAt:new Date().toISOString(),
    method:'Chromium native 4K rendering; SVG glyph bounds, per-container padding, icon and route clearance',
    catalog:{models:catalog.length,passed:catalogPass},assets,
    diagrams:results.map(x=>({file:x.file,status:x.status,pixels:x.pixels,svgSha256:x.svgSha256,pngSha256:x.pngSha256}))};
  await fs.writeFile(path.join(root,'validation.json'),JSON.stringify(validation,null,2)+'\n');
  if(validation.status!=='passed') process.exitCode=1;
})().catch(error=>{console.error(error.message);process.exitCode=1;});
