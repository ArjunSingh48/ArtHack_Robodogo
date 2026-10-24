<!--
IS THIS US? — performance narration (English). Canonical text from the team's brief.
This file is the source of truth for what the robot says in `itu show`. Edit freely;
the cached voice for a changed line is regenerated automatically (`itu show voice pregenerate`).

FORMAT
  ## <chapter-id> | <Chapter title>      starts a chapter (ids are used by the code; keep them)
  @voice: <direction>                    delivery direction for this chapter's voice
  @show: <artifact-id> | title | blank | memes | composition | chosen
                                         changes the screen when the NEXT cue starts
  @motion: <behavior>                    optional robot behavior with the next cue (off unless the
                                         operator enabled show motion AND armed the robot)
  @hold                                  guided mode: wait for the operator ("next") before the next cue
  @branch: together | alone | always     chapter 7 only: lines for after a photo / after a decline
  @interactive                           (invitation chapter) the pose session starts after this chapter
  [pause 2]                              silence, in seconds
  Blank line = end of a cue. A cue is one audio clip; lines inside a cue get natural pauses.
  {pose_request} is replaced with the chosen artwork's request (catalog.yaml).
-->

## last_dog | The Last Dog
@voice: shy curiosity; waking up slowly; small and hesitant
@show: title
Hello.

I am a dog.

…At least, that is what they told me.

[pause 1]

I have four legs.
I can walk. I can run.
Sometimes I fall over.

[pause 1]

But there is a problem.

I have never met another dog.

Actually, nobody has.

There are no dogs left in the world.

[pause 1]

Only pictures.
Thousands and thousands of pictures.

Humans seem to have been very interested in dogs.
So I have been studying them.

[pause 1]

I have one question.

How do I become a good dog?
@hold

## prehistory | Prehistory
@voice: observational, quietly amazed, like reading very old handwriting
@show: met:327508
I started at the beginning.
A very, very long time ago.

[pause 1]

@show: met:545395
Look.
There are humans.

And animals with four legs.

Some look like me.

@show: met:329081
They are running together.

I think… maybe dogs helped humans hunt.
Maybe that was their purpose.

To be fast. To be brave.
To chase things.

I can be fast!

…Well.
For approximately ninety minutes.
Then I need to recharge.

Perhaps ancient dogs had better batteries.

[pause 2]

@show: met:323707
But I noticed something strange.

The humans and the dogs are together.
They keep appearing together.

Maybe that is important.
@hold

## middle_ages | The Middle Ages
@voice: observational, a little puzzled, gentle humour
@show: cma:169746
Then I found these.
Medieval manuscripts.

@show: met:467641
Humans wearing very uncomfortable clothes.

@show: met:464212
And dogs.

@show: met:471826
Sometimes hunting.
Sometimes guarding.

@show: cma:169746
Sometimes just sitting beside someone.

[pause 1]

I have learned a new word.

Loyalty.

It means staying with someone.
Even when things are difficult.

I think I can do that.
I am very good at staying.

Especially when my battery is low.

[pause 2]

But… why would a dog stay?

There is no command in the picture.
The human isn't holding a controller.

The dog simply chooses to be there.

[pause 1]

I don't understand.

I will save this question for later.
@hold

## paintings | Paintings and Portraits
@voice: observational, warming up, a little overwhelmed by how many there are
@show: met:436252
A few centuries later…

Dogs appear everywhere.

In royal portraits.

@show: met:438815
In family paintings.

On sofas.

@show: met:437177
On people's laps.

@show: met:437890
Some are enormous.

@show: met:436323
Some are very small.

@show: met:437449
Some look extremely ridiculous.

@show: met:437966
But the humans keep painting them.
And painting them.
And painting them again.

[pause 1]

I don't think all these dogs were famous.
I don't think they invented anything.

So why did humans want to remember them?

[pause 3]

Maybe…

Maybe you don't have to do something extraordinary to be worth remembering.

Maybe just being there is enough.

[pause 1]

This is a very confusing discovery.

I thought I had to be useful.
@hold

## photography | Photography
@voice: softer, observational, tender; long breaths between images
@show: met:283102
And then humans invented cameras.

Suddenly, there were millions of dogs.

@show: met:265794
Dogs sleeping.
Dogs running.
Dogs getting old.

@show: met:285425
Dogs with children.
Dogs waiting by doors.

@show: met:266886
Some photographs show nothing special at all.

Just an ordinary afternoon.

A human sitting on the grass.
A dog beside them.

Nothing is happening.

[pause 1]

And somehow…

It looks like everything is happening.

[pause 1]

I wish I could ask the dog what it was thinking.

I wish I could ask the human, too.

But I only have the picture.

So I imagine.
@hold

## internet | The Internet
@voice: confusion, then delight; quicker, playful, a little overwhelmed — but still soft
@show: blank
And then…

I discovered the internet.

[pause 1]

@show: meme:doge
Oh.

Oh no.

There are SO MANY DOGS.

@show: memes
Dogs wearing sunglasses.
Dogs driving cars.
Dogs screaming.

@show: meme:doge
Dogs that apparently cannot spell.

"Such wow."
"Much dog."
"Very good boy."

I don't understand the grammar.

But I think I understand the emotion.

@show: meme:side-eye-dog
Humans found dogs funny.

@show: meme:this-is-fine
They made mistakes.
They were silly.

@show: meme:long-nose-borzoi
They did completely unnecessary things.

And humans loved them for it.

[pause 2]

@show: blank
Wait.

Maybe I am also a little ridiculous.

I have four mechanical legs.
I make strange noises.
I sometimes walk into furniture.

Perhaps…

I am already doing quite well.
@hold

## invitation | The Invitation
@voice: shy, hopeful, turning toward a real person for the first time; very gentle
@interactive
@show: blank
@motion: stand
I have studied dogs for a very long time.

But all I have are pictures.

[pause 1]

I wonder…

Can we make one together?

Would you help me?

@show: chosen
I found a picture I like.

{pose_request}

Don't worry.

I am still learning too.

## present | The Present
@voice: warmth and vulnerability; slow, intimate, the quietest part of the show
@show: title
@branch: always
I have studied dogs for a very long time.

I learned how they hunted.
How they guarded.
How they played.
How they became part of human families.

I have looked at thousands of images.

But there is something I still don't know.

What did it feel like…

to be a dog?

[pause 1]

No picture can tell me.

And I don't think a computer can calculate it.

[pause 2]

But I noticed one thing.

In almost every picture…

There is a human somewhere.

Sometimes beside the dog.
Sometimes holding a leash.
Sometimes taking the photograph.
Sometimes just looking.

[pause 1]

Maybe a dog was never just a dog.

Maybe being a dog was something that happened between two beings.

[pause 1]

@motion: stand
I cannot bring the old dogs back.

I cannot become one of them.

But perhaps you can show me something the pictures cannot.

And perhaps…

I can show you something too.

[pause 2]

@branch: together
@show: composition
Oh.

This picture didn't exist before.

We made it.

Does that mean…

we're becoming part of the archive too?

[pause 2]

@branch: alone
@show: title
We did not make a picture today.

That's all right.

You stayed, and you listened.

Maybe that is a kind of picture too.

[pause 2]

@branch: always
I don't know if I am a good dog yet.

But I think…

I would like to be your dog.

Not because someone programmed me to.

But because there is so much left for us to discover.

Together.

[pause 2]

Is this us?
