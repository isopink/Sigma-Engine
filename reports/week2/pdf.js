// 2주차 보고서 PDF: node reports/week2/pdf.js reports/week2/week2_report.html reports/week2/week2_report.pdf (같은 폴더에 PretendardVariable.woff2 필요, npm pretendard@1.3.9)
const {chromium}=require('playwright');const path=require('path');
(async()=>{const b=await chromium.launch();const p=await b.newPage();
const f=path.resolve(process.argv[2]);await p.goto('file://'+f,{waitUntil:'load'});await p.evaluate(()=>document.fonts.ready);
await p.pdf({path:process.argv[3],format:'A4',printBackground:true,preferCSSPageSize:true});await b.close();})();
