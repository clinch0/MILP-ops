/** Real Chromium acceptance, isolated scenario copy; artifacts stay under debug. */
import {chromium} from '../scenario-editor/node_modules/playwright/index.mjs';
import {spawn} from 'node:child_process';
import {mkdir,cp,mkdtemp,writeFile,readFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const out=path.resolve(root,process.env.EDITOR_TEST_OUTPUT||'debug/runs/editor-browser');
await mkdir(out,{recursive:true});
const isolated=await mkdtemp(path.join(out,'project-'));
await cp(path.join(root,'scenarios'),path.join(isolated,'scenarios'),{recursive:true});
const port=Number(process.env.EDITOR_TEST_PORT||28390), base=`http://127.0.0.1:${port}`;
const server=spawn(process.env.PYTHON||'python',['-m','clinic.scenario_editor','--root',isolated,'--data',path.join(isolated,'data'),'--static',path.join(root,'scenario-editor/dist'),'--port',String(port)],{cwd:root,env:{...process.env,PYTHONPATH:path.join(root,'runtime/src'),EDITOR_PASSWORD_FILE:path.join(isolated,'secrets/password.json'),EDITOR_BOOTSTRAP_PASSWORD:'admin',AUDIO_BANK_DIR:path.join(root,'data/audio/prompts')}});
let logs='';server.stdout.on('data',d=>logs+=d);server.stderr.on('data',d=>logs+=d);
let browser;const errors=[];const checks=[];let stage='startup';
try{
  for(let i=0;i<80;i++){try{if((await fetch(base+'/health')).ok)break}catch{}await new Promise(r=>setTimeout(r,100))}
  stage='unauthenticated-api';
  assert.equal((await fetch(base+'/api/project')).status,401);checks.push('API rejects unauthenticated project read');
  stage='browser-launch';
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH===undefined?'/opt/google/chrome/chrome':(process.env.CHROME_PATH||undefined),headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1600,height:1000},deviceScaleFactor:1});page.on('pageerror',e=>errors.push(String(e)));
  const slowdown=Number(process.env.EDITOR_TEST_CPU_SLOWDOWN||1);assert.ok(Number.isFinite(slowdown)&&slowdown>=1&&slowdown<=20);
  if(slowdown>1){const cdp=await page.context().newCDPSession(page);await cdp.send('Emulation.setCPUThrottlingRate',{rate:slowdown})}
  stage='login';
  await page.goto(base);await page.getByLabel('Пароль администратора').fill('admin');await page.getByRole('button',{name:'Подключиться',exact:true}).click();
  await page.locator('.react-flow__node').first().waitFor();await page.waitForTimeout(500);
  await page.screenshot({path:path.join(out,'01-constructor.png'),fullPage:true});checks.push('Password login and real React Flow graph');
  stage='create-flow';
  await page.getByRole('button',{name:/^Сценарии/}).click();await page.getByRole('button',{name:/Новый сценарий/}).click();await page.getByPlaceholder('Например, запись на диспансеризацию').fill('Проверка браузером');await page.getByRole('button',{name:'Создать сценарий',exact:true}).click();
  await page.waitForTimeout(400);
  stage='edit-flow';
  await page.locator('[data-id="scenario_1.start"]').click();
  await page.getByLabel('Текст сообщения').fill('Здравствуйте! Сценарий для записи к терапевту.');
  await page.getByLabel('Название блока',{exact:true}).fill('Запись к терапевту');
  stage='validate-flow';
  await page.getByRole('button',{name:'Проверить',exact:true}).click();await page.getByText('Все связи в порядке').waitFor();await page.getByLabel('Закрыть окно').click();
  stage='save-flow';
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();await page.getByText('Проект сохранён',{exact:true}).waitFor();
  const project=await page.evaluate(async()=>await (await fetch('/api/project')).json());
  assert.equal(project.flows.find(f=>f.name==='scenario_1').states.find(s=>s.name==='start').statements[0].text,'Здравствуйте! Сценарий для записи к терапевту.');checks.push('Create, edit, validate and save without code');
  stage='indexed-search';
  await page.getByLabel('Поиск по всем сценариям').fill('Запись к терапевту');await page.locator('.search-result').first().waitFor();await page.screenshot({path:path.join(out,'02-indexed-search.png'),fullPage:true});await page.locator('.search-result').first().click();checks.push('Indexed cross-scenario search navigates to block');
  stage='dtmf-condition';
  await page.locator('.transition-row').first().click();await page.locator('.keypad button').filter({hasText:/^0$/}).click();
  await page.screenshot({path:path.join(out,'03-dtmf-transition.png'),fullPage:true});
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();await page.getByText('Проект сохранён',{exact:true}).waitFor();
  const after=await page.evaluate(async()=>await (await fetch('/api/project')).json());assert.deepEqual(after.flows.find(f=>f.name==='scenario_1').states.find(s=>s.name==='start').edges[0].values,['1','0']);checks.push('DTMF condition changed through keypad and persisted');
  stage='add-block';
  await page.getByRole('button',{name:'Блоки',exact:true}).click();await page.locator('.palette-end').click();
  // A disconnected new block becomes the nearby-view center. Reveal the full
  // graph through its actual UI before connecting it to the old source.
  await page.getByRole('button',{name:'Все блоки',exact:true}).click();
  await page.getByRole('button',{name:'Упорядочить',exact:true}).click();await page.waitForTimeout(700);await page.locator('.react-flow__controls-fitview').click();await page.waitForTimeout(300);
  stage='connect-blocks';
  const from=page.locator('[data-id="scenario_1.start"] [data-handleid="new"]');
  const to=page.locator('[data-id="scenario_1.end_1"] .react-flow__handle.target');
  stage='connect-drag-source';
  await page.locator('[data-id="scenario_1.start"]').hover();
  await from.hover();
  const sourceBox=await from.boundingBox(),targetBox=await to.boundingBox();
  assert.ok(sourceBox&&targetBox,'Both connection handles must be visible');
  await page.mouse.move(sourceBox.x+sourceBox.width/2,sourceBox.y+sourceBox.height/2);
  await page.mouse.down();
  stage='connect-drag-target';
  await page.mouse.move(targetBox.x+targetBox.width/2,targetBox.y+targetBox.height/2,{steps:20});
  await page.mouse.up();
  stage='connect-edge-visible';
  await page.locator('.port-any').waitFor();
  stage='connect-save';
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();await page.getByText('Проект сохранён',{exact:true}).waitFor();
  const connected=await page.evaluate(async()=>await (await fetch('/api/project')).json());
  assert.ok(connected.flows.find(f=>f.name==='scenario_1').states.find(s=>s.name==='start').edges.some(e=>e.target==='end_1'));
  await page.screenshot({path:path.join(out,'05-connected-blocks.png'),fullPage:true});checks.push('Added a palette block and connected its handles with the mouse');
  stage='restore-version';
  await page.getByLabel('История версий').click();await page.getByRole('button',{name:'Восстановить',exact:true}).first().waitFor();assert.ok(await page.getByRole('button',{name:'Восстановить',exact:true}).count()>0);await page.screenshot({path:path.join(out,'04-history.png'),fullPage:true});
  await page.locator('.version button:not([disabled])').first().click();await page.getByText('Предыдущая версия восстановлена.').waitFor();checks.push('History restore button works');
  if(process.env.TEST_AUDIO_BANK==='1'){
    stage='audio-playback';
    await page.getByRole('button',{name:'Аудиофразы',exact:true}).click();await page.getByLabel('Поиск аудиофраз').waitFor();
    const audio=page.locator('.audio-list audio').first();await audio.waitFor();await audio.evaluate(el=>{el.load();el.play()});
    await page.waitForFunction(()=>{const el=document.querySelector('.audio-list audio');return el&&!el.paused&&el.currentTime>0});
    await page.screenshot({path:path.join(out,'06-audio-bank-playing.png'),fullPage:true});checks.push('Audio-bank panel plays a real prerecorded WAV');
  }
  stage='javascript-errors';
  assert.deepEqual(errors,[]);checks.push('No browser JavaScript errors');
  await writeFile(path.join(out,'result.json'),JSON.stringify({status:'passed',checks,errors,base,isolated},null,2)+'\n');console.log(JSON.stringify({status:'passed',checks},null,2));
}catch(error){
  const errorType=['Error','TimeoutError','AssertionError'].includes(error.name)?error.name:'Error';
  // Only controlled stage/type reach public annotations, never page text or logs.
  if(process.env.GITHUB_ACTIONS==='true')console.error(`::error title=Visual editor acceptance::${stage}: ${errorType}`);
  await writeFile(path.join(out,'result.json'),JSON.stringify({status:'failed',stage,checks,errors,error:String(error)},null,2));
  if(browser){const pages=browser.contexts().flatMap(c=>c.pages());if(pages[0])try{await pages[0].screenshot({path:path.join(out,'failure.png'),fullPage:true,timeout:5000})}catch{}}
  throw error;
}
finally{if(browser)await browser.close();server.kill('SIGTERM');await writeFile(path.join(out,'server.log'),logs)}
