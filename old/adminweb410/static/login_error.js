<script>
var API_BASE=(function(){var p=location.pathname;if(p.charAt(p.length-1)!=='/'){p=p.slice(0,p.lastIndexOf('/')+1);}return p;})();
function apiPath(u){return API_BASE+u.replace(/^\//,'');}
function goLogin(){location.href=apiPath('/login');}
</script>