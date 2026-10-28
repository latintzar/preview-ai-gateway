/** Real project code supplies its reviewed task ID/input and handles its actual result. */
export class PreviewAI {
  constructor({gatewayOrigin, project}) {
    const url = new URL(gatewayOrigin);
    if (url.protocol !== 'https:' || url.origin !== gatewayOrigin) throw new Error('Use the configured HTTPS gateway origin.');
    this.origin=gatewayOrigin;this.project=project;this.csrf=null;
  }
  async session() {
    const response=await fetch(this.origin+'/api/session',{method:'POST',credentials:'include',
      headers:{'Content-Type':'application/json'},body:'{}'});
    const data=await response.json();
    if(!response.ok)throw new Error(data.message||'Open the preview again.');
    this.csrf=data.csrf;return data;
  }
  openKeyWindow() {
    window.open(this.origin+'/key?project='+encodeURIComponent(this.project),'_blank','noopener,noreferrer');
  }
  async useMyKey(openWindow=true) {
    if(openWindow)this.openKeyWindow();
    await this.session();
    const deadline=Date.now()+120000;
    while(Date.now()<deadline){
      await new Promise(resolve=>setTimeout(resolve,5000));
      if((await this.session()).has_key)return;
    }
    throw new Error('Return to the preview after adding your key.');
  }
  async run({taskId,input,mode}) {
    const session=await this.session();
    mode=mode||(session.has_key?'own':'shared');
    const response=await fetch(this.origin+'/api/run',{method:'POST',credentials:'include',
      headers:{'Content-Type':'application/json','X-CSRF-Token':this.csrf},
      body:JSON.stringify({task_id:taskId,input,mode,request_id:crypto.randomUUID()})});
    const data=await response.json();
    if(response.ok)return data.result;
    if(['daily_budget_used','shared_disabled','rate_review_required','key_required'].includes(data.code)){
      const useKey=await this.offer(data);
      if(useKey){await this.useMyKey(false);return this.run({taskId,input,mode:'own'});}
    }
    const error=new Error(data.message||'Try this feature again.');error.code=data.code;throw error;
  }
  offer(data) {
    return new Promise(resolve=>{
      const dialog=document.createElement('dialog');dialog.className='atelier-ai-fallback';
      const message=document.createElement('p');
      message.textContent=data.message;dialog.appendChild(message);
      const own=document.createElement('button');own.textContent='Use my key';
      own.onclick=()=>{this.openKeyWindow();dialog.close();dialog.remove();resolve(true);};dialog.appendChild(own);
      const code=document.createElement('a');code.textContent='Get the code';
      const action=data.actions?.find(item=>item.label==='Get the code');
      if(action&&new URL(action.url).origin==='https://github.com'){code.href=action.url;code.target='_blank';code.rel='noopener noreferrer';}
      code.onclick=()=>{dialog.close();dialog.remove();resolve(false);};dialog.appendChild(code);
      dialog.addEventListener('cancel',()=>{dialog.remove();resolve(false);},{once:true});
      document.body.appendChild(dialog);dialog.showModal();
    });
  }
}
