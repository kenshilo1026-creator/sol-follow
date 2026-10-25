"use strict";
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
function sharedCooldown(directory){
  const folder=url=>path.join(directory,crypto.createHash('sha256').update(url).digest('hex'));
  return {
    get(url){
      if(!directory)return 0;
      const dir=folder(url),now=Date.now();let names;
      try{names=fs.readdirSync(dir);}catch(e){if(e.code==='ENOENT')return 0;throw e;}
      let deadline=0;
      for(const name of names){
        if(!/^\d+\.limit$/.test(name))continue;
        const stamp=Number(name.slice(0,-6));
        if(stamp>now)deadline=Math.max(deadline,stamp);
        else {try{fs.unlinkSync(path.join(dir,name));}catch(e){if(e.code!=='ENOENT')throw e;}}
      }
      return deadline;
    },
    set(url,deadline){
      if(!directory)return;
      const dir=folder(url);fs.mkdirSync(dir,{recursive:true});
      const name=String(Math.ceil(deadline))+'.limit';
      fs.closeSync(fs.openSync(path.join(dir,name),'a'));
    }
  };
}
module.exports={sharedCooldown};
