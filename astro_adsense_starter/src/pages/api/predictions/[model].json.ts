import type { APIRoute } from 'astro';
import { getAllModelsPredictionsData } from '../../../lib/dashboard-data';
import { runtimeEnvFromLocals } from '../../../lib/country-master';

const MODELS = ['catboost', 'ensemble', 'logreg'] as const;
type ModelName = (typeof MODELS)[number];

export function getStaticPaths() {
  return MODELS.map((model) => ({ params: { model } }));
}

export const GET: APIRoute = async ({ params, locals }) => {
  const model = params.model as ModelName;
  if (!MODELS.includes(model)) {
    return new Response(JSON.stringify({ error: 'unknown model' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    });
  }
  const env = runtimeEnvFromLocals(locals);
  const all = await getAllModelsPredictionsData(env);
  const rows = all[model] ?? [];
  return new Response(JSON.stringify(rows), {
    headers: {
      'Content-Type': 'application/json',
      'Cache-Control': 'public, max-age=300, s-maxage=300',
    },
  });
};
