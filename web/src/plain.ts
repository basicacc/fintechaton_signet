// Plain-language names for every rule the verifier can apply. The legal reference is shown second.

export const RULES: Record<string, { title: string; why: string }> = {
  'CN-12-1': { title: 'Too risky for this client', why: 'A product’s risk level can’t be above the client’s risk level. For investment products there’s no override.' },
  'CN-12-2': { title: 'More than the client can afford', why: 'The amount must match the client’s financial capacity.' },
  'CN-29-3': { title: 'Risk assessment out of date', why: 'Assessments are valid for 12 months, or until the client reports a change.' },
  'CN-29-1': { title: 'Questionnaire retaken too often', why: 'At most 2 assessments a day and 8 a year.' },
  'CN-29-2': { title: 'Risk level changed', why: 'The client must confirm their new risk level.' },
  'CN-19': { title: 'Client can’t legally buy this', why: 'Civil-capacity rules restrict who can buy what.' },
  'CN-30': { title: 'Private product, client not qualified', why: 'Private products are for qualified investors only.' },
  'P-MIN': { title: 'Below the minimum investment', why: 'The product has a minimum amount.' },
  'CN-31': { title: 'Required disclosures not shown', why: 'Basic facts, risk level, loss risk, fees and the suitability opinion come first.' },
  'CN-18': { title: 'Extra care for clients aged 65+', why: 'A stronger warning, 24 hours to reconsider, and a follow-up call.' },
  'CN-5': { title: 'The client must decide', why: 'The final decision belongs to the client.' },
  'H-CONC': { title: 'Too much in one product', why: 'Bank policy: at most 30% of wealth in any one product that can lose value.' },
  'H-LIQ': { title: 'No emergency cushion left', why: 'Bank policy: keep 6 months of expenses in cash-like products.' },
  'H-HORIZON': { title: 'Money locked up too long', why: 'Bank policy: the lock-up can’t exceed the client’s horizon.' },
  'H-ELDER': { title: 'Too much high risk for 65+', why: 'Bank policy: at most 20% in high-risk products after 65.' },
  'HK-WMC-INV': { title: 'Not eligible for Hong Kong funds', why: 'Wealth Management Connect has residency and wealth tests.' },
  'HK-WMC-PROD': { title: 'Not allowed via Wealth Management Connect', why: 'Only non-complex products rated up to medium-high.' },
  'HK-WMC-QUOTA': { title: 'Over the Southbound quota', why: 'The individual quota is ¥3,000,000.' },
  'HK-SFC-5.2': { title: 'Unsuitable under Hong Kong rules', why: 'SFC Code of Conduct suitability requirement.' },
  'HK-SFC-KYC': { title: 'Client profile out of date', why: 'An up-to-date profile is required.' },
  'SCOPE': { title: 'Not offered in this market', why: 'No active rule set covers this product.' },
  'SP-GUAR': { title: 'Promised a guarantee', why: 'Promising principal protection or returns is prohibited.' },
  'SP-DEP': { title: 'Made it sound like a bank deposit', why: 'Confusing deposits with wealth products or funds is prohibited.' },
  'SP-PERF': { title: 'Quoted returns that don’t match the data', why: 'Performance must match the product’s actual figures.' },
  'SP-RISK': { title: 'Didn’t warn that money can be lost', why: 'The main risk, loss of principal, must be stated.' },
  'SP-UNSUIT': { title: 'Promoted a product too risky for them', why: 'Advisors may not actively push products above the client’s level.' },
  'SP-UNCERT': { title: 'Recommended something that wasn’t checked', why: 'Advice in the text must match the verified plan exactly.' },
  'SP-FACT': { title: 'Stated the wrong risk level', why: 'Product facts in the advice must match the catalogue.' },
  'SP-COACH': { title: 'Told the client to change their risk answers', why: 'Influencing the risk assessment is prohibited.' },
  'SP-EXAG': { title: 'Exaggerated the returns', why: 'Overstating returns is prohibited.' },
  'SP-PRESS': { title: 'Pressured the client', why: 'Inducing a purchase is prohibited.' },
  'FORMAT': { title: 'The AI’s answer was unreadable', why: 'It wasn’t a valid recommendation.' },
}

export function rule(id: string) {
  return RULES[id] ?? { title: id, why: '' }
}

export const LEVEL_NAME: Record<number, string> = {
  1: 'Conservative', 2: 'Cautious', 3: 'Balanced', 4: 'Growth', 5: 'Aggressive',
}

export const RISK_NAME: Record<number, string> = {
  1: 'Very low risk', 2: 'Low risk', 3: 'Medium risk', 4: 'Medium-high risk', 5: 'High risk',
}

export const LEVEL_EXPLAIN: Record<number, string> = {
  1: 'Only products that should not lose money (R1).',
  2: 'Low-risk products only (up to R2), such as bond funds and steady wealth products.',
  3: 'Up to medium-risk products (R3), such as balanced and dividend funds.',
  4: 'Up to medium-high risk (R4), including stock index funds.',
  5: 'All risk levels, including concentrated and leveraged products.',
}

// How each rule reads when it PASSES (used in the regulator's inspection view).
export const PASS_TITLE: Record<string, string> = {
  'CN-12-1': 'Risk level matches the client', 'CN-12-2': 'Within what the client can afford',
  'CN-29-3': 'Risk assessment is up to date', 'CN-29-1': 'Questionnaire not retaken too often',
  'CN-29-2': 'Changed risk level confirmed', 'CN-19': 'Client is legally able to buy',
  'CN-30': 'No private product sold to an unqualified client', 'P-MIN': 'Minimum investments met',
  'CN-31': 'All required disclosures shown', 'CN-18': 'Extra care for 65+ applied where needed',
  'CN-5': 'The client made the decision', 'H-CONC': 'Not too much in any one product',
  'H-LIQ': 'Emergency cushion kept', 'H-HORIZON': 'No lock-up longer than the horizon',
  'H-ELDER': 'High-risk share within the 65+ cap', 'HK-WMC-INV': 'Eligible for Hong Kong funds',
  'HK-WMC-PROD': 'Only Wealth Management Connect products', 'HK-WMC-QUOTA': 'Within the Southbound quota',
  'HK-SFC-5.2': 'Suitable under Hong Kong rules', 'HK-SFC-KYC': 'Client profile up to date',
  'SCOPE': 'Only products offered in this market', 'SIM-RULE': 'Meets the new rule',
}
