'use strict';
// Cooperative scheduling: never delays an execution request or websocket data.
class BackgroundGate {
  constructor(){this.trades=0;this.foreground=0;this.waiters=new Set();}
  get busy(){return this.trades>0||this.foreground>0;}
  setTrades(n){if(!Number.isSafeInteger(n)||n<0)throw Error('invalid-priority');this.trades=n;this.wake();}
  enter(){this.foreground++;}
  leave(){this.foreground--;this.wake();}
  wake(){if(!this.busy){for(const done of this.waiters)done();this.waiters.clear();}}
  async wait(){while(this.busy)await new Promise(done=>this.waiters.add(done));}
}
module.exports={BackgroundGate};
