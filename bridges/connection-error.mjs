// Only diagnostic scalar fields; never request URLs, headers, bodies or messages.
export function connectionErrorDetails(error, depth=0) {
  if (!error || depth>=4) return null;
  const detail={};
  for (const key of ['code','syscall','address','port']) {
    const value=error[key];
    if (typeof value==='number' || typeof value==='string') detail[key]=String(value).slice(0,128);
  }
  const cause=connectionErrorDetails(error.cause,depth+1);
  if (cause) detail.cause=cause;
  if (Array.isArray(error.errors)) detail.errors=error.errors.slice(0,4).map(e=>connectionErrorDetails(e,depth+1));
  return Object.keys(detail).length ? detail : null;
}
