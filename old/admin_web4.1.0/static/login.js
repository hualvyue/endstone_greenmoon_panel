<script>
var API_BASE=(function(){var p=location.pathname;if(p.charAt(p.length-1)!=='/'){p=p.slice(0,p.lastIndexOf('/')+1);}return p;})();
function apiPath(u){return API_BASE+u.replace(/^\//,'');}
function doLogin(){
  var u=document.getElementById('user').value.trim();
  var p=document.getElementById('pass').value;
  var btn=document.getElementById('loginBtn'),msg=document.getElementById('msg');
  btn.disabled=true;msg.textContent='登录中…';
  fetch(apiPath('/api/login'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})})
  .then(function(res){return res.json().catch(function(){return {ok:false,error:'响应异常'};});})
  .then(function(r){
    if(r&&r.ok){location.href=apiPath(r.redirect||'/admin');}
    else{btn.disabled=false;msg.textContent='';location.href=apiPath('/login?e=1');}
  })
  .catch(function(){btn.disabled=false;msg.textContent='';location.href=apiPath('/login?e=1');});
}
document.getElementById('user').addEventListener('keydown',function(ev){if(ev.key==='Enter'){ev.preventDefault();document.getElementById('pass').focus();}});
document.getElementById('pass').addEventListener('keydown',function(ev){if(ev.key==='Enter'){ev.preventDefault();doLogin();}});
</script>