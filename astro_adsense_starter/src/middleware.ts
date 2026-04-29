import type { MiddlewareHandler } from 'astro';
import { getCountryMasterIndex, runtimeEnvFromLocals } from './lib/country-master';

export const onRequest: MiddlewareHandler = async ({ locals, url }, next) => {
  const pathname = url.pathname.toLowerCase();
  if (pathname === '/rankings' || pathname === '/rankings/' || pathname === '/rankigns' || pathname === '/rankigns/') {
    return Response.redirect(new URL('/', url), 301);
  }

  locals.countryMaster = await getCountryMasterIndex(runtimeEnvFromLocals(locals));
  return next();
};
