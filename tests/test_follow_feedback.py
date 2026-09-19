import subprocess
from pathlib import Path


def test_feedback_has_no_attribute_requirement_but_keeps_hard_exclusions():
    module=(Path(__file__).resolve().parents[1]/'bridges/follow-feedback.mjs').as_uri()
    script='''
import assert from 'node:assert/strict';
import {checkFollowFeedback} from MODULE;
const product={sku:'1',seller_id:'2',weight_first_valuation:true,profit_evaluation_only:true,
 valuation_weight_g:100,plugin_detail:{sku:'1'},source_relation:{seller_id:'2'},
 expansion_source:{contract:'flowhub-same-seller-v1',seed_bindings:[{}]},
 price_evidence:{value:50,currency:'CNY',observed_at:100}};
const source={comparebot:{decision:{outcome:'approved'}}};
const deps={engine:{categoryPolicyFor:()=>({eligible:false}),prohibitedCategoryMatch:()=>false},
 rules:{},cfg:{flow_f:{blocked_source_brands:[]}},feedback:{},
 feedbackBlockForProduct:()=>({blocked:false}),feedbackBlockForPair:()=>({blocked:false}),
 blockedImportBrand:()=>false,verifiedPluginSource:()=>false,verifiedPluginEvaluation:()=>false};
assert.deepEqual(checkFollowFeedback(product,source,deps,101),{blocked:false});
assert.deepEqual(checkFollowFeedback(product,source,{...deps,blockedImportBrand:()=>true},101),{rejected:'protected_product'});
assert.deepEqual(checkFollowFeedback(product,source,{...deps,feedbackBlockForPair:()=>({blocked:true})},101),{blocked:true});
assert.deepEqual(checkFollowFeedback(product,source,deps,30000),{rejected:'category_not_prioritized'});
assert.throws(()=>checkFollowFeedback(product,{comparebot:{decision:{outcome:'manual_review'}}},deps,101));
'''.replace('MODULE',repr(module))
    result=subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
