import type { MiddlewareHandler } from 'astro';
import { getCountryMasterIndex, runtimeEnvFromLocals } from './lib/country-master';

export const onRequest: MiddlewareHandler = async ({ locals }, next) => {
  locals.countryMaster = await getCountryMasterIndex(runtimeEnvFromLocals(locals));
  return next();
};
