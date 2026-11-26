// The front page's hero: connected browser windows float up in three lanes,
// the wxrks mark travels between them as the crawler, and word pieces stream
// into a rolling counter. Illustrative — not live crawl data, and it never
// shows a number as if it were one.
//
// Adapted from the "Graphite" edition (marketing-writing-style/output/
// wordcount-webgl/graphite.js). Build: `npm run build` in webgl/ writes
// app/static/hero-webgl.js, which is what the page loads, after first paint.
import * as THREE from 'three';
import {RoundedBoxGeometry} from 'three/addons/geometries/RoundedBoxGeometry.js';
import {RoomEnvironment} from 'three/addons/environments/RoomEnvironment.js';
const host=document.getElementById('home-webgl'),hero=host.closest('.home-hero');
// No WebGL: step aside. The hero is complete without the scene.
let renderer;try{renderer=new THREE.WebGLRenderer({alpha:true,antialias:true,powerPreference:'high-performance'});}catch(e){host.remove();throw e;}
hero.classList.add('has-webgl');
renderer.setPixelRatio(Math.min(devicePixelRatio,1.6));renderer.setClearColor(0,0);renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=.95;host.prepend(renderer.domElement);
const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(34,1,.1,80);camera.position.set(0,0,16);
const pmrem=new THREE.PMREMGenerator(renderer),room=new RoomEnvironment();scene.environment=pmrem.fromScene(room,.035).texture;room.dispose();pmrem.dispose();
scene.add(new THREE.HemisphereLight(0xffffff,0x707886,1.6));const key=new THREE.DirectionalLight(0xffffff,3);key.position.set(-3,5,7);scene.add(key);const rim=new THREE.DirectionalLight(0xd9e4ff,2);rim.position.set(5,-2,-2);scene.add(rim);
const rig=new THREE.Group();scene.add(rig);
const graphite=new THREE.MeshPhysicalMaterial({color:0x373d47,metalness:.8,roughness:.27,clearcoat:1});
const silver=new THREE.MeshPhysicalMaterial({color:0xbfc5cf,metalness:.78,roughness:.23});
const paper=new THREE.MeshPhysicalMaterial({color:0xe0e3e8,metalness:.35,roughness:.3});
const textMat=new THREE.MeshStandardMaterial({color:0x6a7587,metalness:.4,roughness:.4});
const glass=new THREE.MeshPhysicalMaterial({color:0xc1cad7,transmission:.94,roughness:.15,thickness:.22,ior:1.3,transparent:true,opacity:.55});
const wordGeo=new RoundedBoxGeometry(.16,.048,.045,2,.015), dummy=new THREE.Object3D();
function box(w,h,d,mat,r=.04){return new THREE.Mesh(new RoundedBoxGeometry(w,h,d,3,r),mat)}
// A spatial website map: landscape browser windows, URL bars and linked routes.
function label(text,w=512,h=80){const c=document.createElement('canvas');c.width=w;c.height=h;const ctx=c.getContext('2d');ctx.clearRect(0,0,w,h);ctx.fillStyle='#465263';ctx.font='28px monospace';ctx.textBaseline='middle';ctx.fillText(text,16,h/2);const tex=new THREE.CanvasTexture(c);tex.colorSpace=THREE.SRGBColorSpace;const m=new THREE.Mesh(new THREE.PlaneGeometry(1.65,.26),new THREE.MeshBasicMaterial({map:tex,transparent:true,depthWrite:false}));return m;}
const routes=['example.com/','/about','/services','/blog','/contact','/pricing','/shop','/product','/team','/work','/article','/help'];
const locations=routes.map((_,i)=>{const row=Math.floor(i/3),col=i%3;return [(col-1)*2.05,2.25-row*1.12,Math.sin(i*.9)*.28];});
const pageScale=.71;
function websiteTexture(i){
 const c=document.createElement('canvas');c.width=640;c.height=340;const x=c.getContext('2d');x.fillStyle='#f1f3f6';x.fillRect(0,0,640,340);
 const rect=(a,b,w,h,col='#d5dbe3')=>{x.fillStyle=col;x.fillRect(a,b,w,h);};
 const lines=(a,b,w,n=3)=>{for(let j=0;j<n;j++)rect(a,b+j*17,w*(j===n-1?.67:1),5,'#929dab');};
 const title=(a,b,w)=>rect(a,b,w,13,'#3b4657');
 const tile=(a,b,w,h)=>{rect(a,b,w,h);rect(a,b,w,h*.62,'#a4afbe');};
 switch(i){
 case 0:rect(0,0,640,210,'#3b4657');rect(35,50,295,23,'#eef1f7');rect(35,91,230,10,'#bac6d7');rect(35,139,92,28,'#889ab2');for(let j=0;j<3;j++)tile(30+j*205,235,175,80);break;
 case 1:tile(25,25,255,270);title(315,40,260);lines(315,85,280,7);break;
 case 2:title(25,20,320);for(let j=0;j<3;j++){tile(25+j*205,68,180,170);lines(25+j*205,265,165,3);}break;
 case 3:title(25,20,180);tile(25,60,355,230);for(let j=0;j<3;j++){rect(405,60+j*83,75,62);lines(495,65+j*83,115,3);}break;
 case 4:title(25,30,210);lines(25,80,220,5);for(let j=0;j<3;j++)rect(320,40+j*70,290,42);rect(320,267,130,30,'#465469');break;
 case 5:title(155,15,330);for(let j=0;j<3;j++){rect(25+j*205,55,180,260,j===1?'#526074':'#e0e5ec');rect(48+j*205,85,100,25,'#aab6c6');for(let k=0;k<5;k++)rect(48+j*205,145+k*23,130,5,'#a0adbf');}break;
 case 6:rect(20,20,100,300);for(let j=0;j<6;j++)tile(145+j%3*158,20+Math.floor(j/3)*157,140,135);break;
 case 7:tile(25,20,330,300);title(390,35,205);lines(390,80,200,5);rect(390,200,95,32,'#7c899c');rect(390,260,210,40,'#3b4657');break;
 case 8:title(200,15,240);for(let j=0;j<8;j++){x.fillStyle='#9ba9bc';x.beginPath();x.arc(85+j%4*155,95+Math.floor(j/4)*145,38,0,Math.PI*2);x.fill();lines(40+j%4*155,150+Math.floor(j/4)*145,90,1);}break;
 case 9:tile(20,20,355,300);tile(395,20,220,135);tile(395,175,220,145);break;
 case 10:title(70,20,440);rect(70,57,500,103,'#9ba8b9');lines(70,190,340,8);rect(450,190,120,105);break;
 case 11:rect(100,20,440,46);for(let j=0;j<4;j++){rect(50,90+j*56,540,38);rect(68,105+j*56,280,5,'#8492a5');}break;
 case 12:title(25,20,280);for(let j=0;j<3;j++){rect(25,65+j*87,65,65,'#8998ad');title(115,72+j*87,260);lines(115,101+j*87,390,2);}break;
 case 13:rect(0,0,640,112,'#66768b');rect(30,35,355,25,'#eef1f7');for(let j=0;j<3;j++){title(30,145+j*64,210+j*35);rect(500,140+j*64,105,27);lines(30,171+j*64,340,1);}break;
 case 14:title(190,20,260);for(let j=0;j<4;j++){rect(30+j%2*310,75+Math.floor(j/2)*127,55,55,'#74849b');title(105+j%2*310,80+Math.floor(j/2)*127,165);lines(105+j%2*310,112+Math.floor(j/2)*127,175,3);}break;
 }
 const tex=new THREE.CanvasTexture(c);tex.colorSpace=THREE.SRGBColorSpace;return tex;
}
const pages=[],sweeps=[],borders=[];
for(let i=0;i<routes.length;i++){
 const g=new THREE.Group();g.position.set(...locations[i]);g.rotation.y=i%2?.12:-.1;g.rotation.z=((i%3)-1)*.025;g.scale.setScalar(pageScale);
 g.add(box(2.05,1.38,.075,paper,.06));
 const chrome=box(2.02,.27,.02,silver,.04);chrome.position.set(0,.545,.047);g.add(chrome);
 for(let j=0;j<3;j++){const d=new THREE.Mesh(new THREE.SphereGeometry(.023,8,8),graphite);d.position.set(-.87+j*.09,.55,.07);g.add(d);}
 const url=label(routes[i]);url.scale.setScalar(.7);url.position.set(.22,.55,.075);g.add(url);
 const content=new THREE.Mesh(new THREE.PlaneGeometry(1.91,1.04),new THREE.MeshBasicMaterial({map:websiteTexture(i)}));content.position.set(0,-.13,.063);g.add(content);
 const border=new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(2.1,1.43,.09)),new THREE.LineBasicMaterial({color:0x667e9f,transparent:true,opacity:.12}));g.add(border);borders.push(border);
 const scanBar=box(.027,1.07,.025,silver,.008);scanBar.position.set(-.96,-.1,.105);g.add(scanBar);sweeps.push(scanBar);rig.add(g);pages.push(g);
}
const logoTexture=await new THREE.TextureLoader().loadAsync('/static/brand/logo-x-green.svg');logoTexture.colorSpace=THREE.SRGBColorSpace;
const logoMaterial=new THREE.MeshBasicMaterial({map:logoTexture,transparent:true,side:THREE.DoubleSide});
const paths=[],signals=[];
for(let i=0;i<routes.length;i++){
 const start=new THREE.Vector3(...locations[i]),end=new THREE.Vector3(...locations[(i+3)%routes.length]);start.z+=.18;end.z+=.18;
 const mid=start.clone().lerp(end,.5);mid.z+=.7;mid.x+=(i%2?.35:-.35);
 const curve=new THREE.QuadraticBezierCurve3(start,mid,end);paths.push(curve);
 const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(80)),new THREE.LineBasicMaterial({color:0x8090a5,transparent:true,opacity:.32}));
 const signal=new THREE.Group();
 const body=box(.32,.32,.055,new THREE.MeshStandardMaterial({color:0x02ff33,roughness:.3}),.04);signal.add(body);
 const face=new THREE.Mesh(new THREE.PlaneGeometry(.32,.32),logoMaterial);face.position.z=.029;signal.add(face);
 rig.add(signal);signals.push(signal);
}
const collector=new THREE.Group();collector.position.set(.0,3.25,1.1);rig.add(collector);

// Rolling digit columns convey counting without presenting a fixed measured total.
const digitCanvas=document.createElement('canvas');digitCanvas.width=768;digitCanvas.height=176;
const digitContext=digitCanvas.getContext('2d');
const digitTexture=new THREE.CanvasTexture(digitCanvas);digitTexture.colorSpace=THREE.SRGBColorSpace;
const digitDisplay=new THREE.Mesh(new THREE.PlaneGeometry(2.1,.48),new THREE.MeshBasicMaterial({map:digitTexture,transparent:true,depthWrite:false}));collector.add(digitDisplay);
function paintDigits(time){
 const ctx=digitContext;ctx.clearRect(0,0,768,176);ctx.font='500 112px monospace';ctx.textAlign='center';ctx.textBaseline='middle';
 for(let i=0;i<5;i++){
 const value=time*(.7+i*.36)+i*2.13,base=Math.floor(value),fraction=value-base,x=155+i*112;
 ctx.save();ctx.beginPath();ctx.rect(x-55,16,110,144);ctx.clip();
 for(let k=-1;k<=2;k++){const y=88+(k-fraction)*128;const alpha=Math.max(0,1-Math.abs(y-88)/110);ctx.fillStyle='rgba(48,54,64,'+alpha+')';ctx.fillText(String((base+k+100)%10),x,y);}
 ctx.restore();
 }
 digitTexture.needsUpdate=true;
}
pages.forEach(g=>g.traverse(o=>{if(o.material){o.material=o.material.clone();o.userData.baseOpacity=o.material.opacity;o.material.transparent=true;}}));
const extractedWordGeo=new RoundedBoxGeometry(.25,.068,.062,2,.018);
const extractedWordMat=new THREE.MeshStandardMaterial({color:0x303845,metalness:.18,roughness:.48});
const n=135,swarm=new THREE.InstancedMesh(extractedWordGeo,extractedWordMat,n);swarm.instanceMatrix.setUsage(THREE.DynamicDrawUsage);rig.add(swarm);
// Full-hero floating field: the copy region gets only tiny low-contrast fragments.
const dustCount=110, dust=new THREE.InstancedMesh(wordGeo,new THREE.MeshStandardMaterial({color:0xacb3be,metalness:.3,roughness:.5,transparent:true,opacity:.35}),dustCount);scene.add(dust);
const debris=[];for(let i=0;i<14;i++){const mesh=i%3===0?new THREE.Mesh(new THREE.TorusGeometry(.1,.013,6,24),silver):box(.15,.15,.025,paper,.025);scene.add(mesh);debris.push(mesh);}
const pointer=new THREE.Vector2();let paused=matchMedia('(prefers-reduced-motion: reduce)').matches,visible=true,t=0,last=0,halfW=10,halfH=5,mobile=false;
// It runs continuously; only a reduced-motion preference holds it still.
let dirty=true;matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change',e=>{paused=e.matches;dirty=true});
hero.addEventListener('pointermove',e=>{const r=hero.getBoundingClientRect();pointer.set((e.clientX-r.left)/r.width-.5,(e.clientY-r.top)/r.height-.5)});hero.addEventListener('pointerleave',()=>pointer.set(0,0));new IntersectionObserver(e=>visible=e[0].isIntersecting).observe(hero);
function resize(){dirty=true;const w=host.clientWidth,h=host.clientHeight;mobile=w<801;camera.aspect=w/h;camera.position.z=16;camera.updateProjectionMatrix();renderer.setSize(w,h);halfH=Math.tan(THREE.MathUtils.degToRad(17))*camera.position.z;halfW=halfH*camera.aspect;
 // On a phone the canvas is its own band under the copy (style.css), so the scene is centred in it rather than pushed below a full-hero canvas.
 rig.position.set(mobile?0:halfW*.46,mobile?-.35:0,0);rig.scale.setScalar(mobile?.92:1.03);}new ResizeObserver(resize).observe(host);resize();
const ease=x=>x*x*(3-2*x),v=new THREE.Vector3();let priorPhase=-1,lastCount=-1;
function draw(ms){requestAnimationFrame(draw);const dt=Math.min((ms-last)/1000,.04);last=ms;if(!visible||document.hidden)return;
 // Reduced motion: one still frame, then nothing until something changes.
 if(paused&&!dirty)return;dirty=false;if(!paused)t+=dt*1.6;
 rig.rotation.set(-.12+Math.sin(t*.17)*.065+(paused?0:pointer.y*.12),-.3+Math.sin(t*.12)*.12+(paused?0:pointer.x*.22),-.08+Math.sin(t*.2)*.045);
 // Each lane runs continuously; recycle only while completely faded out.
 pages.forEach((g,i)=>{
 const col=i%3,slot=Math.floor(i/3),phase=(slot*1.28+t*.56+col*.29)%5.12;
 const y=2.5-phase,fade=Math.min(1,phase/.55,(5.12-phase)/.65);
 g.position.set((col-1)*2.05+Math.sin(t*.3+i)*.025,y,-.12+Math.sin(i*.9)*.12);
 g.scale.setScalar(pageScale);g.rotation.set(0,(col-1)*.045+Math.sin(t*.2+i)*.025,(col-1)*.018);
 g.traverse(o=>{if(o.material)o.material.opacity=o.userData.baseOpacity*fade;});
 sweeps[i].visible=false;borders[i].material.opacity=.15*fade;
 });
 signals.forEach((m,i)=>{m.visible=i<3;if(!m.visible)return;m.position.set((i-1)*2.05,0,.25);m.rotation.z=0;m.scale.setScalar(1);});
 collector.rotation.y=0;collector.scale.setScalar(1);
 paintDigits(t);
 for(let i=0;i<n;i++){
 const q=(t*.48+Math.floor(i/3)/(n/3))%1,col=i%3;
 dummy.position.set((col-1)*2.05,0,.27).lerp(collector.position,q);
 dummy.position.x+=Math.sin(q*Math.PI)*Math.sin(i*2.4)*.2;
 dummy.position.z+=Math.sin(q*Math.PI)*.5;dummy.rotation.set(q*.3,q*2,.1*Math.sin(i));
 dummy.scale.setScalar(Math.min(1,q*9,(1-q)*9)*.68);dummy.updateMatrix();swarm.setMatrixAt(i,dummy.matrix);
 }swarm.instanceMatrix.needsUpdate=true;
 for(let i=0;i<dustCount;i++){const x=Math.sin(i*128.13)*halfW*.97,y=Math.cos(i*73.71)*halfH*.92;const copyZone=mobile?y>0:x<halfW*.06;dummy.position.set(x+Math.sin(t*.13+i)*.12,y+Math.cos(t*.19+i)*.12,-1+Math.sin(i*1.3));dummy.rotation.set(i+t*.07,i*.4+t*.1,i);dummy.scale.setScalar(copyZone?.18:.45);dummy.updateMatrix();dust.setMatrixAt(i,dummy.matrix);}dust.instanceMatrix.needsUpdate=true;
 debris.forEach((m,i)=>{const a=i/14*Math.PI*2+t*.025;const x=Math.cos(a)*halfW*.91,y=Math.sin(a)*halfH*.83;const small=mobile?y>0:x<halfW*.06;m.position.set(x,y,-.2+Math.sin(i)*.4);m.scale.setScalar(small?.2:.75);m.rotation.set(t*.2+i,t*.14+i*.3,i+t*.07);});
 renderer.render(scene,camera);
}requestAnimationFrame(draw);
